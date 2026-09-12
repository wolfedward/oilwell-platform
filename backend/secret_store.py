"""Operating-system secret retrieval without putting credentials in source files."""

from dataclasses import dataclass, field
import os


class SecretStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredCredential:
    username: str
    password: str = field(repr=False)


def read_windows_credential(target: str) -> StoredCredential:
    """Read a generic credential owned by the current Windows account."""

    if os.name != "nt":
        raise SecretStoreError("Windows Credential Manager is unavailable on this host")
    if not target or len(target) > 256:
        raise SecretStoreError("credential target is invalid")
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        pass

    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
    CREDENTIALW._fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    credential_pointer = PCREDENTIALW()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(credential_pointer)):
        raise SecretStoreError(
            f"credential is unavailable: {target} (Windows error {ctypes.get_last_error()})"
        )
    try:
        credential = credential_pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            password = blob.decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise SecretStoreError("credential password is not valid Unicode") from error
        return StoredCredential(credential.UserName or "", password)
    finally:
        advapi32.CredFree(credential_pointer)
