"""Fake-API tests exercise the writer, not native ACL enforcement.

The explicitly marked native test is the only OS ACL proof and skips on Mac.
"""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from amplifier_module_tool_mcp.windows_artifacts import save_private_bytes
from amplifier_module_tool_mcp.evidence import canonical_json, save_envelope


class WinError(OSError):
    def __init__(self, code):
        super().__init__(f"winerror {code}")
        self.winerror = code


class ACL:
    def __init__(self):
        self.aces = []

    def AddAccessAllowedAceEx(self, revision, flags, mask, sid):
        self.aces.append(((0, flags), mask, sid))

    def GetAceCount(self):
        return len(self.aces)

    def GetAce(self, index):
        return self.aces[index]


class Descriptor:
    def __init__(self):
        self.owner = None
        self.acl = None
        self.control = 0

    def SetSecurityDescriptorOwner(self, owner, defaulted):
        self.owner = owner

    def SetSecurityDescriptorDacl(self, present, acl, defaulted):
        self.acl = acl
        self.control |= 4 if present else 0

    def SetSecurityDescriptorControl(self, mask, value):
        self.control = self.control & ~mask | value

    def GetSecurityDescriptorOwner(self):
        return self.owner

    def GetSecurityDescriptorDacl(self):
        return self.acl

    def GetSecurityDescriptorControl(self):
        return self.control, 1


class Handle:
    def __init__(self, kind, fake):
        self.kind = kind
        self.fake = fake
        self.closed = False
        fake.handles.append(self)

    def Close(self):
        assert not self.closed
        self.closed = True
        self.fake.events.append(("close", self.kind))


@pytest.fixture
def win32(monkeypatch):
    """A unit-only Win32 seam: no claim that these fakes enforce OS privacy."""
    constants = SimpleNamespace(
        FILE_ALL_ACCESS=0x1F01FF, OBJECT_INHERIT_ACE=1, CONTAINER_INHERIT_ACE=2,
        TOKEN_QUERY=8, READ_CONTROL=0x20000, FILE_READ_ATTRIBUTES=0x80,
        FILE_SHARE_READ=1, FILE_SHARE_WRITE=2, OPEN_EXISTING=3, CREATE_NEW=1,
        FILE_FLAG_OPEN_REPARSE_POINT=0x200000,
        FILE_FLAG_BACKUP_SEMANTICS=0x2000000, FILE_ATTRIBUTE_REPARSE_POINT=0x400,
        FILE_ATTRIBUTE_DIRECTORY=0x10, FILE_ATTRIBUTE_NORMAL=0x80,
        GENERIC_WRITE=0x40000000,
    )
    fake = SimpleNamespace(
        constants=constants, handles=[], events=[], descriptors={}, creations=[],
        existing=False, collision=False, bad=None, bad_kind="directory",
        write_error=False, short_write=False, flush_error=False, delete_error=False,
        directory_attributes=0x10, file_attributes=0x80,
    )

    def private(flags):
        sd = Descriptor()
        sd.SetSecurityDescriptorOwner("current", False)
        acl = ACL()
        acl.AddAccessAllowedAceEx(2, flags, constants.FILE_ALL_ACCESS, "current")
        sd.SetSecurityDescriptorDacl(True, acl, False)
        sd.SetSecurityDescriptorControl(0x1000, 0x1000)
        return sd

    fake.descriptors["directory"] = private(3)
    fake.descriptors["file"] = private(0)

    def create_directory(path, sa):
        fake.creations.append(("directory", path, sa))
        if fake.existing:
            raise WinError(183)

    def create_file(path, access, share, sa, disposition, flags, template):
        kind = "directory" if disposition == constants.OPEN_EXISTING else "file"
        fake.creations.append((kind, path, access, share, sa, disposition, flags))
        if kind == "file" and fake.collision:
            raise WinError(80)
        return Handle(kind, fake)

    def security_info(handle, object_type, information):
        fake.events.append(("verify", handle.kind))
        sd = fake.descriptors[handle.kind]
        if fake.bad_kind == handle.kind:
            if fake.bad == "public":
                sd.acl.aces[0] = ((0, 3 if handle.kind == "directory" else 0),
                                 constants.FILE_ALL_ACCESS, "everyone")
            elif fake.bad == "null":
                sd.acl = None
            elif fake.bad == "foreign":
                sd.owner = "foreign"
            elif fake.bad == "extra":
                sd.acl.aces.append(((0, 0), 1, "everyone"))
            elif fake.bad == "unprotected":
                sd.control &= ~0x1000
            elif fake.bad == "mask":
                header, _, sid = sd.acl.aces[0]
                sd.acl.aces[0] = (header, 1, sid)
            elif fake.bad == "deny":
                _, mask, sid = sd.acl.aces[0]
                sd.acl.aces[0] = ((1, 0), mask, sid)
            elif fake.bad == "inherit_only":
                _, mask, sid = sd.acl.aces[0]
                sd.acl.aces[0] = ((0, 8), mask, sid)
        return sd

    def write(handle, data):
        fake.events.append(("write", data))
        if fake.write_error:
            raise WinError(29)
        return 0, len(data) - 1 if fake.short_write else len(data)

    def flush(handle):
        fake.events.append(("flush",))
        if fake.flush_error:
            raise WinError(29)

    def delete(path):
        fake.events.append(("delete", path))
        assert all(h.closed for h in fake.handles if h.kind == "file")
        assert any(not h.closed for h in fake.handles if h.kind == "directory")
        if fake.delete_error:
            raise WinError(5)

    modules = {
        "pywintypes": SimpleNamespace(error=WinError, SECURITY_ATTRIBUTES=SimpleNamespace),
        "win32api": SimpleNamespace(GetCurrentProcess=lambda: "process"),
        "win32con": constants,
        "win32security": SimpleNamespace(
            ACL=ACL, SECURITY_DESCRIPTOR=Descriptor, ACL_REVISION=2,
            ACCESS_ALLOWED_ACE_TYPE=0, SE_DACL_PROTECTED=0x1000,
            SE_DACL_PRESENT=4, SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1,
            DACL_SECURITY_INFORMATION=4, TokenUser=1,
            OpenProcessToken=lambda process, access: Handle("token", fake),
            GetTokenInformation=lambda token, info: ("current", 0),
            ConvertSidToStringSid=lambda sid: sid, GetSecurityInfo=security_info,
        ),
        "win32file": SimpleNamespace(
            CreateDirectory=create_directory, CreateFile=create_file,
            GetFileInformationByHandle=lambda h: (
                fake.directory_attributes if h.kind == "directory"
                else fake.file_attributes,
            ),
            GetFinalPathNameByHandle=lambda h, flags: "\\\\?\\C:\\verified\\private",
            WriteFile=write, FlushFileBuffers=flush, DeleteFile=delete,
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return fake


def call(tmp_path):
    return save_private_bytes(b"exact evidence \x00\xff", tmp_path / "private", "a.json")


def assert_closed(fake):
    assert fake.handles
    assert all(handle.closed for handle in fake.handles)


def test_creation_is_private_exclusive_and_verified_before_write(win32, tmp_path):
    path = call(tmp_path)
    assert path == "\\\\?\\C:\\verified\\private\\a.json"
    directory, opened_directory, file = win32.creations
    for sa, flags in [(directory[2], 3), (file[4], 0)]:
        sd = sa.SECURITY_DESCRIPTOR
        assert sd.owner == "current"
        assert sd.control & 0x1000
        assert sd.acl.aces == [((0, flags), 0x1F01FF, "current")]
        assert sa.bInheritHandle is False
    assert opened_directory[3] == 3  # Explicitly no FILE_SHARE_DELETE (4).
    assert opened_directory[6] == 0x2200000
    assert file[3] == 0
    assert file[5] == win32.constants.CREATE_NEW
    assert file[6] & win32.constants.FILE_FLAG_OPEN_REPARSE_POINT
    assert win32.events[:4] == [
        ("verify", "directory"), ("verify", "file"),
        ("write", b"exact evidence \x00\xff"), ("flush",),
    ]
    assert not any(event[0] == "delete" for event in win32.events)
    assert_closed(win32)


def test_adapter_windows_branch_saves_exact_canonical_bytes(win32, tmp_path, monkeypatch):
    # The real adapter calls the real writer; only OS APIs are faked.
    monkeypatch.setattr("amplifier_module_tool_mcp.evidence.os", SimpleNamespace(name="nt"))
    envelope = {"content": [{"type": "text", "text": "é" * 90000}], "_meta": {"tail": "kept"}}
    expected = canonical_json(envelope).encode("utf-8")
    recovery = save_envelope(envelope, tmp_path / "private")
    assert ("write", expected) in win32.events
    assert recovery["bytes"] == len(expected)
    assert recovery["sha256"] == hashlib.sha256(expected).hexdigest()
    assert recovery["complete"]
    assert recovery["format"] == "mcp-sdk-envelope-json"
    assert_closed(win32)


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize(
    "bad", ["public", "null", "foreign", "extra", "unprotected",
            "mask", "deny", "inherit_only"],
)
def test_unsafe_acl_rejected_before_write(win32, tmp_path, kind, bad):
    win32.existing = True
    win32.bad_kind = kind
    win32.bad = bad
    with pytest.raises(RuntimeError, match="recovery artifact creation failed"):
        call(tmp_path)
    assert not any(e[0] == "write" for e in win32.events)
    assert sum(e[0] == "delete" for e in win32.events) == (kind == "file")
    assert_closed(win32)


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize("reparse", [True, False])
def test_wrong_type_or_reparse_rejected_before_write(win32, tmp_path, kind, reparse):
    attribute = 0x400 if reparse else (0x80 if kind == "directory" else 0x10)
    setattr(win32, kind + "_attributes", attribute)
    with pytest.raises(RuntimeError, match="reparse point|wrong file type"):
        call(tmp_path)
    assert not any(e[0] == "write" for e in win32.events)
    assert_closed(win32)


def test_collision_is_never_deleted(win32, tmp_path):
    win32.collision = True
    with pytest.raises(RuntimeError, match="winerror 80"):
        call(tmp_path)
    assert not any(e[0] in ("write", "delete") for e in win32.events)
    assert_closed(win32)


@pytest.mark.parametrize("error", ["write_error", "short_write", "flush_error"])
def test_incomplete_owned_file_removed_and_handles_closed(win32, tmp_path, error):
    setattr(win32, error, True)
    with pytest.raises(RuntimeError, match="recovery artifact creation failed"):
        call(tmp_path)
    assert [e for e in win32.events if e[0] == "delete"] == [
        ("delete", "\\\\?\\C:\\verified\\private\\a.json")
    ]
    assert_closed(win32)


def test_cleanup_failure_is_explicit(win32, tmp_path):
    win32.write_error = win32.delete_error = True
    with pytest.raises(RuntimeError, match="incomplete recovery artifact removal failed"):
        call(tmp_path)
    assert sum(e[0] == "delete" for e in win32.events) == 1
    assert_closed(win32)


@pytest.mark.parametrize("name", ["../a", "a:b", "a\\b", "NUL", "a.", "a\0b"])
def test_invalid_leaf_never_calls_os(win32, tmp_path, name):
    with pytest.raises(RuntimeError, match="invalid leaf"):
        save_private_bytes(b"evidence", tmp_path / "private", name)
    assert not win32.creations
    assert not win32.handles


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows ACLs")
def test_native_windows_private_roundtrip_collision_and_unsafe_directory(tmp_path):
    import win32api
    import win32con
    import win32file
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    data = b"native recovery \x00\xff"
    directory = tmp_path / "private"
    path = Path(save_private_bytes(data, directory, "a.json"))
    assert path.is_absolute()
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(data).digest()
    for target, inheritance in [(directory, 3), (path, 0)]:
        handle = win32file.CreateFile(
            str(target), win32con.READ_CONTROL | win32con.FILE_READ_ATTRIBUTES,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
            win32con.OPEN_EXISTING, win32con.FILE_FLAG_OPEN_REPARSE_POINT
            | win32con.FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        try:
            sd = win32security.GetSecurityInfo(
                handle, win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            assert win32security.ConvertSidToStringSid(
                sd.GetSecurityDescriptorOwner()
            ) == win32security.ConvertSidToStringSid(sid)
            assert sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
            acl = sd.GetSecurityDescriptorDacl()
            assert acl is not None and acl.GetAceCount() == 1
            ace = acl.GetAce(0)
            assert ace[0] == (win32security.ACCESS_ALLOWED_ACE_TYPE, inheritance)
            assert ace[1] == win32con.FILE_ALL_ACCESS
            assert win32security.ConvertSidToStringSid(ace[2]) == (
                win32security.ConvertSidToStringSid(sid)
            )
        finally:
            handle.Close()
    with pytest.raises(RuntimeError, match="recovery artifact creation failed"):
        save_private_bytes(b"replacement", directory, "a.json")
    assert path.read_bytes() == data
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    # Make the negative fixture deterministically public, not dependent on temp ACLs.
    acl = win32security.ACL()
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION, 3, win32con.FILE_ALL_ACCESS, everyone
    )
    win32security.SetNamedSecurityInfo(
        str(unsafe), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )
    with pytest.raises(RuntimeError, match="unexpected trustee"):
        save_private_bytes(data, unsafe, "must-not-exist.json")
    assert not (unsafe / "must-not-exist.json").exists()