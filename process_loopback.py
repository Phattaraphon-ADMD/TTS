import ctypes
import threading
import time
from ctypes import POINTER, Structure, Union, byref, c_longlong, c_ubyte, c_ushort
from ctypes.wintypes import DWORD

from comtypes import COMObject, COMMETHOD, GUID, HRESULT, IUnknown
from pycaw.api.audioclient import IAudioClient
from pycaw.api.audioclient.depend import WAVEFORMATEX

PROCESS_LOOPBACK_DEVICE = "VAD\\Process_Loopback"
ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_INCLUDE_TREE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002
VT_BLOB = 65
SAMPLES_PER_SECOND = 48000
CHANNEL_COUNT = 2
BITS_PER_SAMPLE = 16
BYTES_PER_FRAME = CHANNEL_COUNT * BITS_PER_SAMPLE // 8
POLL_INTERVAL_SECONDS = 0.005
ACTIVATION_TIMEOUT_SECONDS = 10


def get_audio_processes():
    import os

    from pycaw.pycaw import AudioUtilities

    processes = {}
    for session in AudioUtilities.GetAllSessions():
        process_id = session.ProcessId
        if process_id <= 0 or process_id == os.getpid():
            continue
        try:
            process = session.Process
            if process is None:
                continue
            process_name = process.name()
        except Exception:
            continue

        is_active = session.State == 1
        current = processes.get(process_id)
        display_name = session.DisplayName.strip() or process_name
        if current is None:
            processes[process_id] = {
                "pid": process_id,
                "process_name": process_name,
                "display_name": display_name,
                "active": is_active,
            }
        elif is_active:
            current["active"] = True

    return sorted(
        processes.values(),
        key=lambda process: (not process["active"], process["process_name"].casefold(), process["pid"]),
    )


class _ProcessLoopbackParams(Structure):
    _fields_ = [("TargetProcessId", DWORD), ("ProcessLoopbackMode", DWORD)]


class _ActivationParamsUnion(Union):
    _fields_ = [("ProcessLoopbackParams", _ProcessLoopbackParams)]


class _AudioClientActivationParams(Structure):
    _fields_ = [("ActivationType", DWORD), ("ProcessLoopbackParams", _ActivationParamsUnion)]


class _Blob(Structure):
    _fields_ = [("cbSize", DWORD), ("pBlobData", POINTER(c_ubyte))]


class _PropVariantUnion(Union):
    _fields_ = [("blob", _Blob), ("alignment", c_longlong * 2)]


class _PropVariant(Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("value", _PropVariantUnion),
    ]


class _IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            ( ["out"], POINTER(POINTER(c_ubyte)), "data"),
            (["out"], POINTER(DWORD), "framesAvailable"),
            (["out"], POINTER(DWORD), "flags"),
            (["out"], POINTER(ctypes.c_ulonglong), "devicePosition"),
            (["out"], POINTER(ctypes.c_ulonglong), "qpcPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], DWORD, "numFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(DWORD), "numFramesInNextPacket")),
    )


class _IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetActivateResult",
            (["out"], POINTER(HRESULT), "activationResult"),
            (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
        ),
    )


class _IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "ActivateCompleted",
            (["in"], POINTER(_IActivateAudioInterfaceAsyncOperation), "operation"),
        ),
    )


class _IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = ()


class _ActivationHandler(COMObject):
    _com_interfaces_ = [_IActivateAudioInterfaceCompletionHandler, _IAgileObject]

    def __init__(self):
        super().__init__()
        self.completed = threading.Event()
        self.audio_client = None
        self.error = None

    def ActivateCompleted(self, operation):
        try:
            activation_result, activated_interface = operation.GetActivateResult()
            if activation_result < 0:
                raise OSError(f"Process loopback activation failed: 0x{activation_result & 0xFFFFFFFF:08X}")
            self.audio_client = activated_interface.QueryInterface(IAudioClient)
        except Exception as error:
            self.error = error
        finally:
            self.completed.set()
        return 0


def _activate_process_loopback(process_id):
    activation_params = _AudioClientActivationParams()
    activation_params.ActivationType = ACTIVATION_TYPE_PROCESS_LOOPBACK
    activation_params.ProcessLoopbackParams.ProcessLoopbackParams.TargetProcessId = process_id
    activation_params.ProcessLoopbackParams.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_INCLUDE_TREE
    )
    activation_bytes = (c_ubyte * ctypes.sizeof(activation_params)).from_buffer_copy(
        activation_params
    )
    activate_params = _PropVariant()
    activate_params.vt = VT_BLOB
    activate_params.blob.cbSize = ctypes.sizeof(activation_params)
    activate_params.blob.pBlobData = ctypes.cast(activation_bytes, POINTER(c_ubyte))

    handler = _ActivationHandler()
    handler_interface = handler.QueryInterface(_IActivateAudioInterfaceCompletionHandler)
    operation = POINTER(_IActivateAudioInterfaceAsyncOperation)()
    activate_audio_interface = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
    activate_audio_interface.argtypes = [
        ctypes.c_wchar_p,
        POINTER(GUID),
        POINTER(_PropVariant),
        POINTER(_IActivateAudioInterfaceCompletionHandler),
        POINTER(POINTER(_IActivateAudioInterfaceAsyncOperation)),
    ]
    activate_audio_interface.restype = HRESULT
    result = activate_audio_interface(
        PROCESS_LOOPBACK_DEVICE,
        byref(IAudioClient._iid_),
        byref(activate_params),
        handler_interface,
        byref(operation),
    )
    if result < 0:
        raise OSError(f"ActivateAudioInterfaceAsync failed: 0x{result & 0xFFFFFFFF:08X}")
    if not handler.completed.wait(ACTIVATION_TIMEOUT_SECONDS):
        raise TimeoutError("หมดเวลารอ Windows Process Loopback activation")
    if handler.error is not None:
        raise handler.error
    return handler.audio_client


def capture_process_audio(process_id, write_frames, stop_event):
    if process_id <= 0:
        raise ValueError("Process ID ต้องมากกว่า 0")

    ole32 = ctypes.WinDLL("Ole32.dll")
    initialize_com = ole32.CoInitializeEx
    initialize_com.argtypes = [ctypes.c_void_p, DWORD]
    initialize_com.restype = HRESULT
    uninitialize_com = ole32.CoUninitialize
    com_result = initialize_com(None, 0)
    if com_result < 0:
        raise OSError(f"COM initialization failed: 0x{com_result & 0xFFFFFFFF:08X}")

    audio_client = None
    capture_client = None
    try:
        audio_client = _activate_process_loopback(process_id)
        audio_format = WAVEFORMATEX()
        audio_format.wFormatTag = 1
        audio_format.nChannels = CHANNEL_COUNT
        audio_format.nSamplesPerSec = SAMPLES_PER_SECOND
        audio_format.wBitsPerSample = BITS_PER_SAMPLE
        audio_format.nBlockAlign = BYTES_PER_FRAME
        audio_format.nAvgBytesPerSec = SAMPLES_PER_SECOND * BYTES_PER_FRAME
        audio_format.cbSize = 0
        audio_client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
            1_000_000,
            0,
            byref(audio_format),
            None,
        )
        capture_service = audio_client.GetService(_IAudioCaptureClient._iid_)
        capture_client = capture_service.QueryInterface(_IAudioCaptureClient)
        audio_client.Start()

        while not stop_event.is_set():
            packet_frames = capture_client.GetNextPacketSize()
            if packet_frames == 0:
                stop_event.wait(POLL_INTERVAL_SECONDS)
                continue

            (
                data_pointer,
                frames_available,
                flags,
                _device_position,
                _qpc_position,
            ) = capture_client.GetBuffer()
            try:
                byte_count = frames_available * BYTES_PER_FRAME
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    audio_frames = bytes(byte_count)
                else:
                    audio_frames = ctypes.string_at(data_pointer, byte_count)
                write_frames(audio_frames)
            finally:
                capture_client.ReleaseBuffer(frames_available)
    finally:
        if audio_client is not None:
            try:
                audio_client.Stop()
            except OSError:
                pass
        uninitialize_com()