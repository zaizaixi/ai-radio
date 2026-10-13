"""音频播放：直接调 Windows waveOut，做到可打断、可选设备、可调音量。

为什么不用 winsound：
  * winsound 不支持"播放内存里的 WAV + 异步"，两者不能同时用
  * winsound 没法选输出设备（直播需要把声音送到虚拟声卡）
  * winsound 没法精确打断

音量是通过直接缩放 PCM 采样实现的，**不会动你的系统音量**，
所以直播时你听歌的音量不受影响。

非 Windows 平台会自动退化成静音播放（只按时间等待），方便开发调试。
"""

from __future__ import annotations

import array
import ctypes
import io
import logging
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("radio.audio")

IS_WINDOWS = sys.platform == "win32"

# ---- waveOut 常量 ----
WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 0x0001
WHDR_DONE = 0x00000001
CALLBACK_NULL = 0x00000000
MMSYSERR_NOERROR = 0
MAXPNAMELEN = 32

_VOLUME_TABLE_CACHE: dict[int, array.array] = {}


# ---------------------------------------------------------------------------
# WAV 解析与音量处理
# ---------------------------------------------------------------------------


@dataclass
class WavData:
    channels: int
    sample_rate: int
    sample_width: int
    pcm: bytes

    @property
    def duration(self) -> float:
        frame = max(1, self.channels * self.sample_width)
        return len(self.pcm) / frame / max(1, self.sample_rate)

    @property
    def is_16bit_pcm(self) -> bool:
        return self.sample_width == 2


def parse_wav(data: bytes) -> WavData:
    """把 WAV 字节解析成裸 PCM。"""
    with wave.open(io.BytesIO(data)) as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    return WavData(channels=channels, sample_rate=rate, sample_width=width, pcm=frames)


def _volume_table(percent: int) -> array.array:
    """建一张 16bit 采样值的缩放查找表。

    表按下标 0..65535 对应采样值 -32768..32767 排列，
    所以用的时候要 +32768 偏移（负数下标会绕到表尾，是个很容易踩的坑）。
    """
    table = _VOLUME_TABLE_CACHE.get(percent)
    if table is None:
        factor = percent / 100.0
        table = array.array(
            "h",
            [max(-32768, min(32767, int(value * factor)))
             for value in range(-32768, 32768)],
        )
        _VOLUME_TABLE_CACHE[percent] = table
    return table


def apply_volume(pcm: bytes, percent: int) -> bytes:
    """按百分比缩放 16bit PCM；100 表示原样返回。"""
    percent = max(0, min(200, int(percent)))
    if percent == 100 or not pcm:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    table = _volume_table(percent)
    scaled = array.array("h", (table[s + 32768] for s in samples))
    return scaled.tobytes()


# ---------------------------------------------------------------------------
# 设备枚举
# ---------------------------------------------------------------------------


@dataclass
class AudioDevice:
    index: int
    name: str


# 常见虚拟声卡的名字特征。命中说明这台机器能把声音"只送给观众"。
VIRTUAL_CABLE_HINTS = (
    "cable input", "cable output", "vb-audio", "vb-cable",
    "voicemeeter", "virtual audio", "虚拟声卡", "vac ",
    "stereo mix", "立体声混音", "loopback",
)


def is_virtual_cable(name: str) -> bool:
    low = (name or "").lower()
    return any(hint in low for hint in VIRTUAL_CABLE_HINTS)


def find_virtual_cables() -> list["AudioDevice"]:
    """找出可以用来把 AI 声音单独送给 OBS 的虚拟设备。"""
    return [d for d in list_devices() if is_virtual_cable(d.name)]


def list_devices() -> list[AudioDevice]:
    """列出可用的播放设备。"""
    if not IS_WINDOWS:
        return []
    try:
        mm = _winmm()
    except Exception:
        return []

    class WAVEOUTCAPS(ctypes.Structure):
        _fields_ = [
            ("wMid", ctypes.c_ushort),
            ("wPid", ctypes.c_ushort),
            ("vDriverVersion", ctypes.c_uint),
            ("szPname", ctypes.c_wchar * MAXPNAMELEN),
            ("dwFormats", ctypes.c_uint),
            ("wChannels", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("dwSupport", ctypes.c_uint),
        ]

    count = mm.waveOutGetNumDevs()
    out: list[AudioDevice] = []
    for index in range(count):
        caps = WAVEOUTCAPS()
        # winmm 只导出 waveOutGetDevCapsW / ...A，ctypes 不会自动补后缀
        rc = mm.waveOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
        if rc == MMSYSERR_NOERROR:
            out.append(AudioDevice(index, caps.szPname))
    return out


# ---------------------------------------------------------------------------
# waveOut 播放器
# ---------------------------------------------------------------------------

_winmm_instance = None
_winmm_lock = threading.Lock()


def _winmm():
    """惰性加载并声明 winmm 函数签名。"""
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
        mm.waveOutGetNumDevs.restype = ctypes.c_uint
        mm.waveOutGetDevCapsW.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        mm.waveOutGetDevCapsW.restype = ctypes.c_uint
        mm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                                   ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_uint]
        mm.waveOutOpen.restype = ctypes.c_uint
        mm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        mm.waveOutPrepareHeader.restype = ctypes.c_uint
        mm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        mm.waveOutWrite.restype = ctypes.c_uint
        mm.waveOutReset.argtypes = [ctypes.c_void_p]
        mm.waveOutReset.restype = ctypes.c_uint
        mm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        mm.waveOutUnprepareHeader.restype = ctypes.c_uint
        mm.waveOutClose.argtypes = [ctypes.c_void_p]
        mm.waveOutClose.restype = ctypes.c_uint

        mm._WAVEFORMATEX = WAVEFORMATEX  # type: ignore[attr-defined]
        mm._WAVEHDR = WAVEHDR            # type: ignore[attr-defined]
        _winmm_instance = mm
        return mm


class WavPlayer:
    """播放 WAV；同一时刻只播一条，新的播放会打断旧的。"""

    def __init__(self, device: str | int | None = None, volume: int = 100) -> None:
        self.volume = int(volume)
        self.device = self._parse_device(device)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._handle: int | None = None
        self._playing = threading.Event()
        self.backend = "winmm" if IS_WINDOWS else "null"

    @staticmethod
    def _parse_device(device: str | int | None) -> int:
        if device in (None, "", "-1"):
            return WAVE_MAPPER
        try:
            return int(device)
        except (TypeError, ValueError):
            return WAVE_MAPPER

    # ------------------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    def stop(self) -> None:
        """立刻停下当前正在播的声音。"""
        self._stop_event.set()
        handle = self._handle
        if handle and IS_WINDOWS:
            try:
                _winmm().waveOutReset(handle)
            except Exception:
                pass

    # ------------------------------------------------------------------

    def play(self, wav_bytes: bytes, volume: int | None = None,
             stop_check: threading.Event | None = None) -> bool:
        """播放一段 WAV。返回 True 表示完整播完，False 表示被打断。

        这个方法会阻塞到播完，所以请放在线程里调用。
        """
        try:
            wav = parse_wav(wav_bytes)
        except Exception as exc:
            log.error("WAV 解析失败: %s", exc)
            return False

        pcm = apply_volume(wav.pcm, self.volume if volume is None else volume)
        if not pcm:
            return True

        self._stop_event.clear()
        self._playing.set()
        try:
            if self.backend == "winmm":
                return self._play_winmm(wav, pcm, stop_check)
            return self._play_silent(wav, stop_check)
        finally:
            self._playing.clear()
            self._handle = None

    # ------------------------------------------------------------------

    def _play_winmm(self, wav: WavData, pcm: bytes,
                    stop_check: threading.Event | None) -> bool:
        mm = _winmm()
        fmt = mm._WAVEFORMATEX(  # type: ignore[attr-defined]
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=wav.channels,
            nSamplesPerSec=wav.sample_rate,
            nAvgBytesPerSec=wav.sample_rate * wav.channels * wav.sample_width,
            nBlockAlign=wav.channels * wav.sample_width,
            wBitsPerSample=wav.sample_width * 8,
            cbSize=0,
        )

        handle = ctypes.c_void_p()
        rc = mm.waveOutOpen(ctypes.byref(handle), self.device, ctypes.byref(fmt),
                            None, None, CALLBACK_NULL)
        if rc != MMSYSERR_NOERROR:
            log.error("打开音频设备失败（错误码 %s），改用静音计时", rc)
            return self._play_silent(wav, stop_check)

        self._handle = handle
        # 必须保住这两块内存，直到播放结束
        buffer = ctypes.create_string_buffer(pcm, len(pcm))
        header = mm._WAVEHDR(  # type: ignore[attr-defined]
            lpData=ctypes.cast(buffer, ctypes.c_void_p),
            dwBufferLength=len(pcm),
        )

        try:
            rc = mm.waveOutPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
            if rc != MMSYSERR_NOERROR:
                log.error("waveOutPrepareHeader 失败：%s", rc)
                return False
            rc = mm.waveOutWrite(handle, ctypes.byref(header), ctypes.sizeof(header))
            if rc != MMSYSERR_NOERROR:
                log.error("waveOutWrite 失败：%s", rc)
                return False

            deadline = time.monotonic() + wav.duration + 5.0
            while True:
                if header.dwFlags & WHDR_DONE:
                    return True
                if self._stop_event.is_set() or (stop_check is not None and stop_check.is_set()):
                    mm.waveOutReset(handle)
                    return False
                if time.monotonic() > deadline:
                    log.warning("播放超时，强制停止")
                    mm.waveOutReset(handle)
                    return False
                time.sleep(0.004)
        finally:
            try:
                mm.waveOutUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
            except Exception:
                pass
            try:
                mm.waveOutClose(handle)
            except Exception:
                pass

    def _play_silent(self, wav: WavData, stop_check: threading.Event | None) -> bool:
        """没有音频后端时，按时间等待，保证整条流水线逻辑一致。"""
        deadline = time.monotonic() + wav.duration
        while time.monotonic() < deadline:
            if self._stop_event.is_set() or (stop_check is not None and stop_check.is_set()):
                return False
            time.sleep(0.01)
        return True


# ---------------------------------------------------------------------------
# 流式播放：把连续到达的 PCM 块喂给声卡，中间不留缝
# ---------------------------------------------------------------------------


class StreamPlayer:
    """边收边播。

    为什么需要它：
        ZipVoice 在 CPU 上合成比实时慢（RTF 2 左右）。
        如果等整段合成完再播，就要干等十几秒；
        如果切成小段分别合成再播，段与段之间又会有空档（这正是"断断续续"的来源）。
        所以改成：整段一次合成，音频一边生成一边往声卡里写，
        靠 write() 的阻塞天然形成背压 —— 合成和播放自己就对上了节奏。

    用法::

        p = StreamPlayer(volume=100)
        p.open(24000)
        p.write(pcm_chunk)      # 可以反复调用，满了会阻塞
        p.drain()               # 等播完
        p.close()
    """

    def __init__(self, device: str | int | None = None, volume: int = 100,
                 slots: int = 6, slot_seconds: float = 0.25) -> None:
        self.device = WavPlayer._parse_device(device)
        self.volume = int(volume)
        self.slots = max(2, int(slots))
        self.slot_seconds = float(slot_seconds)
        self.backend = "winmm" if IS_WINDOWS else "null"

        self.sample_rate = 0
        self._handle = None
        self._mm = None
        self._buffers: list = []
        self._headers: list = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._opened = False
        self._written = 0

    # ------------------------------------------------------------------

    def open(self, sample_rate: int, channels: int = 1, sample_width: int = 2) -> None:
        self.close()
        self.sample_rate = int(sample_rate)
        self.channels = channels
        self.sample_width = sample_width
        self._stop.clear()
        self._written = 0

        if self.backend != "winmm" or sample_width != 2:
            self._opened = True
            return

        mm = _winmm()
        self._mm = mm
        fmt = mm._WAVEFORMATEX(  # type: ignore[attr-defined]
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=channels,
            nSamplesPerSec=sample_rate,
            nAvgBytesPerSec=sample_rate * channels * sample_width,
            nBlockAlign=channels * sample_width,
            wBitsPerSample=sample_width * 8,
            cbSize=0,
        )
        handle = ctypes.c_void_p()
        rc = mm.waveOutOpen(ctypes.byref(handle), self.device, ctypes.byref(fmt),
                            None, None, CALLBACK_NULL)
        if rc != MMSYSERR_NOERROR:
            log.warning("流式播放打不开音频设备（%s），退化成静音计时", rc)
            self.backend = "null"
            self._opened = True
            return

        self._handle = handle
        size = max(2048, int(sample_rate * channels * sample_width * self.slot_seconds))
        size -= size % (channels * sample_width)
        self._buffers = []
        self._headers = []
        # 关键：prepare 之后 dwFlags 是 WHDR_PREPARED，不是 WHDR_DONE。
        # 所以不能只看 DONE 判断"槽位空闲"，否则第一次写就会永久死等。
        self._used: list[bool] = []
        for _ in range(self.slots):
            buffer = ctypes.create_string_buffer(size)
            header = mm._WAVEHDR(  # type: ignore[attr-defined]
                lpData=ctypes.cast(buffer, ctypes.c_void_p),
                dwBufferLength=size,
            )
            rc = mm.waveOutPrepareHeader(handle, ctypes.byref(header),
                                         ctypes.sizeof(header))
            if rc != MMSYSERR_NOERROR:
                log.warning("waveOutPrepareHeader 失败：%s", rc)
            self._buffers.append(buffer)
            self._headers.append(header)
            self._used.append(False)
        self._opened = True

    # ------------------------------------------------------------------

    def _free_slot(self, wait: bool = True) -> int | None:
        """找一个可以复用的缓冲槽。

        没用过的槽位直接可用；用过的要等它播完（WHDR_DONE）。
        """
        while True:
            for index, header in enumerate(self._headers):
                if not self._used[index]:
                    return index
                if header.dwFlags & WHDR_DONE:
                    return index
            if self._stop.is_set() or not wait:
                return None
            time.sleep(0.004)

    def write(self, pcm: bytes) -> bool:
        """写一块 PCM 进去。缓冲满了会阻塞（这就是背压）。返回 False 表示被打断。"""
        if not pcm or self._stop.is_set():
            return False
        if self.volume != 100:
            pcm = apply_volume(pcm, self.volume)

        if self.backend != "winmm":
            time.sleep(len(pcm) / max(1, self.sample_rate * 2))
            return not self._stop.is_set()

        with self._lock:
            offset = 0
            size = self._buffers[0].raw.__len__() if self._buffers else len(pcm)
            while offset < len(pcm):
                index = self._free_slot()
                if index is None:
                    return False
                chunk = pcm[offset:offset + size]
                offset += len(chunk)
                chunk = chunk[: len(chunk) - (len(chunk) % (self.channels * self.sample_width))]
                if not chunk:
                    break
                self._buffers[index].raw = chunk + b"\x00" * (size - len(chunk))
                header = self._headers[index]
                header.dwBufferLength = len(chunk)
                # 只能清 WHDR_DONE！把 dwFlags 整个置 0 会连 WHDR_PREPARED
                # 一起清掉，waveOutWrite 就会返回 WAVERR_UNPREPARED(34)。
                header.dwFlags &= ~WHDR_DONE
                rc = self._mm.waveOutWrite(self._handle, ctypes.byref(header),
                                           ctypes.sizeof(header))
                if rc != MMSYSERR_NOERROR:
                    log.warning("waveOutWrite 失败：%s", rc)
                    return False
                self._used[index] = True
                self._written += len(chunk)
            return True

    # ------------------------------------------------------------------

    def drain(self, timeout: float | None = None) -> None:
        """等已经写进去的音频播完。"""
        if self.backend != "winmm" or not self._headers:
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            with self._lock:
                pending = [i for i, h in enumerate(self._headers)
                           if self._used[i] and not (h.dwFlags & WHDR_DONE)]
            if not pending:
                return
            if deadline is not None and time.monotonic() > deadline:
                return
            time.sleep(0.02)

    @property
    def written_seconds(self) -> float:
        frame = max(1, self.channels * self.sample_width) if self._opened else 2
        return self._written / frame / max(1, self.sample_rate)

    def stop(self) -> None:
        self._stop.set()
        if self._handle and self._mm is not None:
            try:
                self._mm.waveOutReset(self._handle)
            except Exception:
                pass

    def close(self) -> None:
        self.stop()
        if self._handle and self._mm is not None:
            try:
                self._mm.waveOutReset(self._handle)
            except Exception:
                pass
            for header in self._headers:
                try:
                    self._mm.waveOutUnprepareHeader(self._handle,
                                                    ctypes.byref(header),
                                                    ctypes.sizeof(header))
                except Exception:
                    pass
            try:
                self._mm.waveOutClose(self._handle)
            except Exception:
                pass
        self._handle = None
        self._headers = []
        self._buffers = []
        self._opened = False


# ---------------------------------------------------------------------------
# 备用：用 winsound 播（极少数 waveOut 打不开的机器）
# ---------------------------------------------------------------------------


class WinsoundPlayer:
    """退路方案：写临时文件后用 winsound 异步播放。"""

    def __init__(self, cache_dir: Path, volume: int = 100) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.volume = int(volume)
        self.backend = "winsound"
        self._counter = 0
        self._lock = threading.Lock()

    def stop(self) -> None:
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    @property
    def is_playing(self) -> bool:
        return False

    def play(self, wav_bytes: bytes, volume: int | None = None,
             stop_check: threading.Event | None = None) -> bool:
        import winsound

        try:
            wav = parse_wav(wav_bytes)
        except Exception:
            return False
        pcm = apply_volume(wav.pcm, self.volume if volume is None else volume)

        with self._lock:
            self._counter += 1
            path = self.cache_dir / f"clip_{self._counter:06d}.wav"
        _write_wav(path, wav, pcm)

        try:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as exc:
            log.error("winsound 播放失败：%s", exc)
            return False

        deadline = time.monotonic() + wav.duration
        while time.monotonic() < deadline:
            if stop_check is not None and stop_check.is_set():
                self.stop()
                return False
            time.sleep(0.01)
        return True


def _write_wav(path: Path, wav: WavData, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(wav.channels)
        handle.setsampwidth(wav.sample_width)
        handle.setframerate(wav.sample_rate)
        handle.writeframes(pcm)


def make_player(volume: int = 100, device: str | int | None = None,
                cache_dir: Path | None = None):
    """挑一个能用的播放器。"""
    if IS_WINDOWS:
        try:
            _winmm()
            return WavPlayer(device=device, volume=volume)
        except Exception as exc:
            log.warning("waveOut 不可用（%s），改用 winsound", exc)
        if cache_dir is not None:
            return WinsoundPlayer(cache_dir, volume=volume)
    return WavPlayer(device=device, volume=volume)
