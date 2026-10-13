"""麦克风录音（纯 ctypes 调 winmm waveIn）。

为什么需要它：
    ZipVoice 的克隆样本必须是 16 位 PCM WAV。
    但 Windows 自带的「录音机」存的是 m4a，Python 标准库解不了。
    为了不让你去装 Audacity，干脆在软件里直接录。

实现方式：预先把一批小缓冲（每块 0.1 秒）挂到录音设备上，
然后轮询哪些块录满了，边录边算音量，最后拼成一段 WAV。
"""

from __future__ import annotations

import array
import ctypes
import logging
import struct
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("radio.recorder")

IS_WINDOWS = sys.platform == "win32"

MMSYSERR_NOERROR = 0
WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0x00000000
WHDR_DONE = 0x00000001
MAXPNAMELEN = 32

# 优先尝试的采样率（ZipVoice 用 24kHz，但录音设备不一定支持）
PREFERRED_RATES = (24000, 16000, 44100, 48000)

_CHUNK_SECONDS = 0.1

_winmm_instance = None
_winmm_lock = threading.Lock()


def _winmm():
    """惰性加载 winmm 并声明 waveIn 相关签名。"""
    global _winmm_instance
    with _winmm_lock:
        if _winmm_instance is not None:
            return _winmm_instance

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [
                ("wFormatTag", ctypes.c_ushort),
                ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint),
                ("nAvgBytesPerSec", ctypes.c_uint),
                ("nBlockAlign", ctypes.c_ushort),
                ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort),
            ]

        class WAVEHDR(ctypes.Structure):
            _fields_ = [
                ("lpData", ctypes.c_void_p),
                ("dwBufferLength", ctypes.c_uint),
                ("dwBytesRecorded", ctypes.c_uint),
                ("dwUser", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("dwLoops", ctypes.c_uint),
                ("lpNext", ctypes.c_void_p),
                ("reserved", ctypes.c_void_p),
            ]

        mm = ctypes.WinDLL("winmm", use_last_error=True)
        mm.waveInGetNumDevs.restype = ctypes.c_uint
        mm.waveInGetDevCapsW.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        mm.waveInGetDevCapsW.restype = ctypes.c_uint
        mm.waveInOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                                  ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_uint]
        mm.waveInOpen.restype = ctypes.c_uint
        mm.waveInPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        mm.waveInPrepareHeader.restype = ctypes.c_uint
        mm.waveInUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        mm.waveInUnprepareHeader.restype = ctypes.c_uint
        mm.waveInAddBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        mm.waveInAddBuffer.restype = ctypes.c_uint
        mm.waveInStart.argtypes = [ctypes.c_void_p]
        mm.waveInStart.restype = ctypes.c_uint
        mm.waveInStop.argtypes = [ctypes.c_void_p]
        mm.waveInStop.restype = ctypes.c_uint
        mm.waveInReset.argtypes = [ctypes.c_void_p]
        mm.waveInReset.restype = ctypes.c_uint
        mm.waveInClose.argtypes = [ctypes.c_void_p]
        mm.waveInClose.restype = ctypes.c_uint

        mm._WAVEFORMATEX = WAVEFORMATEX      # type: ignore[attr-defined]
        mm._WAVEHDR = WAVEHDR                # type: ignore[attr-defined]
        _winmm_instance = mm
        return mm


# ---------------------------------------------------------------------------
# 设备枚举
# ---------------------------------------------------------------------------


@dataclass
class InputDevice:
    index: int
    name: str
    channels: int


def list_input_devices() -> list[InputDevice]:
    if not IS_WINDOWS:
        return []
    try:
        mm = _winmm()
    except Exception:
        return []

    class WAVEINCAPSW(ctypes.Structure):
        _fields_ = [
            ("wMid", ctypes.c_ushort),
            ("wPid", ctypes.c_ushort),
            ("vDriverVersion", ctypes.c_uint),
            ("szPname", ctypes.c_wchar * MAXPNAMELEN),
            ("dwFormats", ctypes.c_uint),
            ("wChannels", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
        ]

    out: list[InputDevice] = []
    for index in range(mm.waveInGetNumDevs()):
        caps = WAVEINCAPSW()
        rc = mm.waveInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
        if rc == MMSYSERR_NOERROR:
            out.append(InputDevice(index, caps.szPname, caps.wChannels))
    return out


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1,
               sample_width: int = 2) -> bytes:
    if len(pcm) % 2:
        pcm = pcm[:-1]
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                    byte_rate, block_align, sample_width * 8)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


# ---------------------------------------------------------------------------


class RecorderError(Exception):
    """录音失败。"""


class MicRecorder:
    """录一段麦克风音频。

    用法::

        rec = MicRecorder()
        rec.start(max_seconds=15)
        while rec.recording:
            print(rec.level())        # 顺便画个音量条
            time.sleep(0.05)
        wav = rec.stop()              # 返回 WAV 字节
    """

    def __init__(self, device: int = -1) -> None:
        self.device = WAVE_MAPPER if device in (-1, None) else int(device)
        self.sample_rate = 0
        self.channels = 1
        self._handle = None
        self._chunks: list = []
        self._buffers: list = []
        self._headers: list = []
        self._data = bytearray()
        self._consumed = 0
        self._lock = threading.Lock()
        self.recording = False
        self.started_at = 0.0
        self.max_seconds = 15.0
        self._level = 0.0
        self._peak = 0.0
        self._poll_thread: threading.Thread | None = None

    # ------------------------------------------------------------------

    def start(self, max_seconds: float = 15.0) -> None:
        if not IS_WINDOWS:
            raise RecorderError("录音只在 Windows 上可用")
        mm = _winmm()

        if mm.waveInGetNumDevs() == 0:
            raise RecorderError(
                "系统里没有找到麦克风。\n"
                "  检查一下：设置 → 隐私和安全性 → 麦克风，确认允许应用访问。"
            )

        self.max_seconds = max(3.0, min(60.0, float(max_seconds)))
        self._data = bytearray()
        self._consumed = 0
        self._level = 0.0
        self._peak = 0.0
        self._buffers = []
        self._headers = []

        last_error = None
        handle = ctypes.c_void_p()
        for rate in PREFERRED_RATES:
            fmt = mm._WAVEFORMATEX(  # type: ignore[attr-defined]
                wFormatTag=WAVE_FORMAT_PCM,
                nChannels=1,
                nSamplesPerSec=rate,
                nAvgBytesPerSec=rate * 2,
                nBlockAlign=2,
                wBitsPerSample=16,
                cbSize=0,
            )
            handle = ctypes.c_void_p()
            rc = mm.waveInOpen(ctypes.byref(handle), self.device,
                               ctypes.byref(fmt), None, None, CALLBACK_NULL)
            if rc == MMSYSERR_NOERROR:
                self.sample_rate = rate
                break
            last_error = rc
        else:
            raise RecorderError(
                f"打不开麦克风（错误码 {last_error}）。\n"
                "  可能被其他程序占用，或者设备不支持常见采样率。"
            )

        self._handle = handle
        self.channels = 1

        chunk_bytes = int(self.sample_rate * _CHUNK_SECONDS) * 2
        count = int(self.max_seconds / _CHUNK_SECONDS) + 4
        try:
            for _ in range(count):
                buffer = ctypes.create_string_buffer(chunk_bytes)
                header = mm._WAVEHDR(  # type: ignore[attr-defined]
                    lpData=ctypes.cast(buffer, ctypes.c_void_p),
                    dwBufferLength=chunk_bytes,
                )
                rc = mm.waveInPrepareHeader(handle, ctypes.byref(header),
                                            ctypes.sizeof(header))
                if rc != MMSYSERR_NOERROR:
                    raise RecorderError(f"waveInPrepareHeader 失败：{rc}")
                rc = mm.waveInAddBuffer(handle, ctypes.byref(header),
                                        ctypes.sizeof(header))
                if rc != MMSYSERR_NOERROR:
                    raise RecorderError(f"waveInAddBuffer 失败：{rc}")
                self._buffers.append(buffer)
                self._headers.append(header)

            rc = mm.waveInStart(handle)
            if rc != MMSYSERR_NOERROR:
                raise RecorderError(f"waveInStart 失败：{rc}")
        except Exception:
            self._cleanup()
            raise

        self.recording = True
        self.started_at = time.monotonic()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True,
                                             name="mic-poll")
        self._poll_thread.start()

    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        mm = _winmm()
        while self.recording:
            try:
                self._drain(mm)
            except Exception:
                break
            if time.monotonic() - self.started_at > self.max_seconds + 1.0:
                break
            time.sleep(0.03)

    def _drain(self, mm) -> None:
        with self._lock:
            while self._consumed < len(self._headers):
                header = self._headers[self._consumed]
                if not (header.dwFlags & WHDR_DONE):
                    break
                length = int(header.dwBytesRecorded)
                if length > 0:
                    self._data += self._buffers[self._consumed].raw[:length]
                    self._update_level(self._buffers[self._consumed].raw[:length])
                self._consumed += 1

    def _update_level(self, chunk: bytes) -> None:
        samples = array.array("h")
        samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
        if not samples:
            return
        # 抽样算 RMS，别为了画个音量条把 CPU 吃满
        step = max(1, len(samples) // 256)
        total = 0
        count = 0
        peak = 0
        for index in range(0, len(samples), step):
            value = samples[index]
            total += value * value
            count += 1
            peak = max(peak, abs(value))
        if count:
            rms = (total / count) ** 0.5
            self._level = min(1.0, rms / 32768.0 * 4.0)
            self._peak = max(self._peak, peak / 32768.0)

    # ------------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.recording else 0.0

    @property
    def level(self) -> float:
        return self._level

    @property
    def peak(self) -> float:
        return self._peak

    @property
    def recorded_bytes(self) -> int:
        with self._lock:
            return len(self._data)

    def stop(self) -> bytes:
        """停止录音并返回 WAV 字节。"""
        if not self.recording:
            return b""
        self.recording = False
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=1.0)
            self._poll_thread = None

        mm = _winmm()
        handle = self._handle
        if handle:
            try:
                mm.waveInStop(handle)
            except Exception:
                pass
            try:
                self._drain(mm)          # 把最后一块也收进来
            except Exception:
                pass

        seconds = self.elapsed
        self._cleanup()

        with self._lock:
            pcm = bytes(self._data)
        if len(pcm) < self.sample_rate * 2 * 0.3:
            raise RecorderError(
                "几乎没录到声音。\n"
                "  检查一下麦克风权限（设置 → 隐私和安全性 → 麦克风），"
                " 或者换一个输入设备。"
            )
        log.info("录音完成：%.1f 秒 %s Hz 峰值 %.2f",
                 seconds, self.sample_rate, self._peak)
        return pcm_to_wav(pcm, self.sample_rate, 1)

    def cancel(self) -> None:
        self.recording = False
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=1.0)
            self._poll_thread = None
        self._cleanup()

    def _cleanup(self) -> None:
        mm = None
        try:
            mm = _winmm()
        except Exception:
            pass
        handle = self._handle
        self._handle = None
        if handle and mm is not None:
            try:
                mm.waveInReset(handle)
            except Exception:
                pass
            for header in self._headers:
                try:
                    mm.waveInUnprepareHeader(handle, ctypes.byref(header),
                                             ctypes.sizeof(header))
                except Exception:
                    pass
            try:
                mm.waveInClose(handle)
            except Exception:
                pass
        self._headers = []
        self._buffers = []
