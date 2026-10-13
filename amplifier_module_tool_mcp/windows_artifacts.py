"""Private Windows recovery files, protected at creation rather than afterwards.

Only the leaf directory is pinned/checked; ancestors and same-user/admin activity
are outside this bounded writer's protection. No Win32 imports occur at import.
"""

import ntpath
from pathlib import Path


def save_private_bytes(data: bytes, directory: Path, name: str) -> str:
    """Exclusively save bytes under an owner-only, protected Windows DACL.

    Fail closed on an existing unsafe directory; never repair its permissions.
    The caller owns retention and translates failures to its artifact error type.
    """
    # Reject paths, alternate data streams, and Win32 normalization aliases.
    if (
        not name
        or name in (".", "..")
        or any(char in name for char in '\\/:*?"<>|\0')
        or any(ord(char) < 32 for char in name)
        or name.endswith((" ", "."))
        or name.split(".")[0].upper()
        in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}
    ):
        raise RuntimeError("MCP recovery artifact creation failed: invalid leaf name")
    if not isinstance(data, bytes):
        raise RuntimeError("MCP recovery artifact creation failed: data must be bytes")

    try:
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32security
    except ImportError as exc:
        raise RuntimeError(
            "MCP recovery artifact creation failed: pywin32 is required on Windows"
        ) from exc

    handles = []
    created_path = None
    failure = None
    result = None

    def private_attributes(sid, inheritance):
        acl = win32security.ACL()
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, inheritance, win32con.FILE_ALL_ACCESS, sid
        )
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorOwner(sid, False)
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        descriptor.SetSecurityDescriptorControl(
            win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
        )
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        attributes.bInheritHandle = False
        return attributes

    def verify(handle, sid, is_directory):
        attributes = win32file.GetFileInformationByHandle(handle)[0]
        if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("recovery target is a reparse point")
        if bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY) != is_directory:
            raise OSError("recovery target has the wrong file type")
        descriptor = win32security.GetSecurityInfo(
            handle,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        if owner is None or win32security.ConvertSidToStringSid(owner) != sid:
            raise OSError("recovery target must be owned by the current process SID")
        control = descriptor.GetSecurityDescriptorControl()[0]
        if not control & win32security.SE_DACL_PROTECTED:
            raise OSError("recovery target DACL must be protected")
        acl = descriptor.GetSecurityDescriptorDacl()
        if not control & win32security.SE_DACL_PRESENT or acl is None:
            raise OSError("recovery target must have a non-NULL DACL")
        if acl.GetAceCount() != 1:
            raise OSError("recovery target must have exactly one private allow ACE")
        ace = acl.GetAce(0)
        inheritance = (
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
            if is_directory else 0
        )
        if (
            len(ace) != 3
            or ace[0] != (win32security.ACCESS_ALLOWED_ACE_TYPE, inheritance)
            or ace[1] != win32con.FILE_ALL_ACCESS
            or win32security.ConvertSidToStringSid(ace[2]) != sid
        ):
            raise OSError("recovery target has an unexpected trustee, mask or ACE type/flags")

    try:
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        handles.append(token)
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        sid = win32security.ConvertSidToStringSid(owner)
        directory = directory.expanduser().absolute()
        # No evidence bytes in parents. The leaf must be private from creation.
        directory.parent.mkdir(parents=True, exist_ok=True)
        inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        try:
            win32file.CreateDirectory(
                str(directory), private_attributes(owner, inheritance)
            )
        except pywintypes.error as exc:
            if exc.winerror != 183:  # ERROR_ALREADY_EXISTS; verify, never repair.
                raise
        directory_handle = win32file.CreateFile(
            str(directory),
            win32con.READ_CONTROL | win32con.FILE_READ_ATTRIBUTES,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_OPEN_REPARSE_POINT
            | win32con.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        handles.append(directory_handle)  # No delete sharing: pin the leaf.
        verify(directory_handle, sid, True)
        final_directory = win32file.GetFinalPathNameByHandle(directory_handle, 0)
        if not ntpath.isabs(final_directory):
            raise OSError("recovery directory handle did not resolve to an absolute path")
        path = ntpath.join(final_directory, name)
        file_handle = win32file.CreateFile(
            path,
            win32con.GENERIC_WRITE | win32con.READ_CONTROL
            | win32con.FILE_READ_ATTRIBUTES,
            0,
            private_attributes(owner, 0),
            win32con.CREATE_NEW,
            win32con.FILE_ATTRIBUTE_NORMAL | win32con.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        created_path = path  # Set only after our exclusive create succeeds.
        handles.append(file_handle)
        verify(file_handle, sid, False)  # Absolutely no bytes before this check.
        status, count = win32file.WriteFile(file_handle, data)
        if status != 0 or count != len(data):
            raise OSError("recovery artifact write did not complete exactly")
        win32file.FlushFileBuffers(file_handle)
        # Close the file before returning; treat a close failure as incomplete.
        file_handle.Close()
        handles.pop()
        result = path
    except Exception as exc:
        failure = exc
    finally:
        # Keep the directory pinned until an owned incomplete file is removed.
        while handles:
            handle = handles.pop()
            try:
                handle.Close()
            except Exception as exc:
                failure = failure or exc
            if created_path is not None and failure is not None:
                try:
                    win32file.DeleteFile(created_path)
                    created_path = None
                except Exception as exc:
                    failure = RuntimeError(
                        f"{failure}; incomplete recovery artifact removal failed: {exc}"
                    )
                    # Do not retry against a potentially replaced pathname.
                    created_path = None
    if failure is not None:
        raise RuntimeError(f"MCP recovery artifact creation failed: {failure}") from failure
    return result