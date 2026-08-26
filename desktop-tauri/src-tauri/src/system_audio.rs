//! Windows 系统播放音频采集。
//!
//! 独立线程通过默认 Render endpoint 的 WASAPI loopback 获取系统混音，要求
//! 音频引擎在 shared mode 下自动转换为 16 kHz / mono / PCM s16le。完整的
//! 2.5 秒非静音窗口会直接进入现有 Rust outbox；停止时不冲刷不足 2.5 秒的尾片。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use tokio::sync::oneshot;

use crate::engine::{now_ms, EngineCommand, EngineEvent, EngineEventEmitter, NewChunk};

const SAMPLE_RATE: u32 = 16_000;
const CHANNELS: u16 = 1;
const BITS_PER_SAMPLE: u16 = 16;
const CHUNK_DURATION_MS: i64 = 2_500;
const CHUNK_FRAMES: usize = SAMPLE_RATE as usize * CHUNK_DURATION_MS as usize / 1_000;
/// 与前端 worklet 一致：以 20 ms 窗口计算 RMS。
const RMS_WINDOW_FRAMES: usize = SAMPLE_RATE as usize * 20 / 1_000;
/// 0.002 * i16 满量程约等于 66；高于当前观测的约 -62 dBFS 噪声底。
const RMS_THRESHOLD_I16: i64 = 66;
/// 一个 2.5 秒分片至少包含 3 个过阈值窗口才视为有声。
const MIN_VOICED_WINDOWS: usize = 3;
const START_TIMEOUT: Duration = Duration::from_secs(5);
const STOP_TIMEOUT: Duration = Duration::from_secs(2);

struct Worker {
    stop: Arc<AtomicBool>,
    alive: Arc<AtomicBool>,
    /// join 句柄放共享槽:stop() 超时后 spawn_blocking 仍可完成 join 并释放
    /// 句柄,而 worker 本体可安全放回全局状态等待下次回收。
    join: Arc<Mutex<Option<JoinHandle<()>>>>,
}

/// Tauri 全局状态：同一时间最多运行一个系统音频采集线程。
#[derive(Default)]
pub struct SystemAudioState {
    worker: Mutex<Option<Worker>>,
}

impl SystemAudioState {
    /// 启动采集并等待 WASAPI 初始化完成。已在运行时返回 `Ok(false)`。
    pub async fn start(
        &self,
        command_tx: tokio::sync::mpsc::UnboundedSender<EngineCommand>,
        emit: EngineEventEmitter,
    ) -> Result<bool, String> {
        let ready_rx = {
            let mut slot = self
                .worker
                .lock()
                .map_err(|_| "系统音频采集状态锁已损坏".to_string())?;

            if let Some(worker) = slot.as_ref() {
                if worker.alive.load(Ordering::Acquire) {
                    return Ok(false);
                }
            }

            // 回收已自然退出的旧线程，避免后续 start 永远被陈旧句柄阻塞。
            if let Some(worker) = slot.take() {
                worker.stop.store(true, Ordering::Release);
                let joined = worker
                    .join
                    .lock()
                    .ok()
                    .and_then(|mut guard| guard.take())
                    .map(|handle| handle.join());
                match joined {
                    Some(Ok(())) => {}
                    Some(Err(_)) => return Err("系统音频采集线程异常退出".to_string()),
                    // 句柄不在槽里:上次 stop() 超时,spawn_blocking 仍在 join。
                    // 线程自带停止标记会退出,这里不阻塞等待(避免与旧 join 死锁)。
                    None => {}
                }
            }

            let stop = Arc::new(AtomicBool::new(false));
            let alive = Arc::new(AtomicBool::new(true));
            let thread_stop = Arc::clone(&stop);
            let thread_alive = Arc::clone(&alive);
            let thread_command_tx = command_tx.clone();
            let (ready_tx, ready_rx) = oneshot::channel();
            let join = match std::thread::Builder::new()
                .name("system-audio-loopback".into())
                .spawn(move || {
                    let mut ready = Some(ready_tx);
                    let result = platform::run(&thread_stop, &thread_command_tx, &mut ready);
                    if let Err(message) = result {
                        if let Some(ready_tx) = ready.take() {
                            let _ = ready_tx.send(Err(message));
                        } else if !thread_stop.load(Ordering::Acquire) {
                            emit(EngineEvent::ServerError {
                                code: "system_audio_capture_failed".into(),
                                message,
                                retry_after_seconds: None,
                                chunk_id: None,
                            });
                        }
                    }
                    thread_alive.store(false, Ordering::Release);
                }) {
                Ok(join) => join,
                Err(error) => return Err(format!("无法创建系统音频采集线程：{error}")),
            };

            *slot = Some(Worker {
                stop,
                alive,
                join: Arc::new(Mutex::new(Some(join))),
            });
            ready_rx
        };

        match tokio::time::timeout(START_TIMEOUT, ready_rx).await {
            Ok(Ok(Ok(()))) => Ok(true),
            Ok(Ok(Err(message))) => {
                let _ = self.stop().await;
                Err(message)
            }
            Ok(Err(_)) => {
                let _ = self.stop().await;
                Err("系统音频采集线程在初始化完成前退出".into())
            }
            Err(_) => {
                let _ = self.stop().await;
                Err("系统音频采集初始化超时".into())
            }
        }
    }

    /// 请求线程停止并等待它退出。采集循环每 10 ms 检查一次停止标记。
    ///
    /// 超时不丢句柄:join 半边存回槽位,线程已带停止标记,会在下个 10ms
    /// 轮询点(或 WASAPI 调用返回后)自行退出;后续 stop()/start() 再次回收。
    pub async fn stop(&self) -> Result<bool, String> {
        let worker = self
            .worker
            .lock()
            .map_err(|_| "系统音频采集状态锁已损坏".to_string())?
            .take();
        let Some(worker) = worker else {
            return Ok(false);
        };

        worker.stop.store(true, Ordering::Release);
        let shared_join = Arc::clone(&worker.join);
        let join = tokio::task::spawn_blocking(move || {
            let handle = shared_join.lock().ok()?.take()?;
            handle.join().ok()
        });
        match tokio::time::timeout(STOP_TIMEOUT, join).await {
            // 句柄已被先前路径取走(Option::None)视作已回收;join 返回 Err 说明
            // 线程 panic;spawn_blocking 自身失败归入最后一条。
            Ok(Ok(Some(()))) | Ok(Ok(None)) => Ok(true),
            Ok(Err(join_error)) => Err(format!("等待系统音频采集线程失败：{join_error}")),
            Err(_) => {
                // 超时:spawn_blocking 仍持有 join 锁等待线程。把 worker(含
                // 共享 join 槽)放回全局状态,停止标记已置位,等 spawn_blocking
                // 里的 join 完成后句柄被释放;下次 stop() take 到的是已退出/
                // 仍卡住的 worker,重试 join 或由 start() 回收。
                eprintln!("系统音频采集线程未能在 2 秒内退出,保留句柄等待后续回收");
                let mut slot = self
                    .worker
                    .lock()
                    .map_err(|_| "系统音频采集状态锁已损坏".to_string())?;
                // 仅当槽位为空时放回:期间 start() 可能已装入新 worker。
                if slot.is_none() {
                    *slot = Some(worker);
                }
                Err("系统音频采集线程未能在 2 秒内退出".into())
            }
        }
    }
}

impl Drop for SystemAudioState {
    fn drop(&mut self) {
        let Ok(slot) = self.worker.get_mut() else {
            return;
        };
        if let Some(worker) = slot.take() {
            worker.stop.store(true, Ordering::Release);
            // 不 join:进程退出场景下阻塞 Drop 可能挂死在 WASAPI/COM 调用;
            // 线程带停止标记自行退出,进程终止时随进程回收。
        }
    }
}

#[derive(Default)]
struct ChunkAssembler {
    samples: Vec<i16>,
}

impl ChunkAssembler {
    fn push(&mut self, mut input: &[i16]) -> Vec<Vec<i16>> {
        let mut completed = Vec::new();
        while !input.is_empty() {
            let remaining = CHUNK_FRAMES - self.samples.len();
            let take = remaining.min(input.len());
            self.samples.extend_from_slice(&input[..take]);
            input = &input[take..];
            if self.samples.len() == CHUNK_FRAMES {
                completed.push(std::mem::replace(
                    &mut self.samples,
                    Vec::with_capacity(CHUNK_FRAMES),
                ));
            }
        }
        completed
    }

    fn discard_tail(&mut self) {
        self.samples.clear();
    }
}

fn is_effectively_silent(samples: &[i16]) -> bool {
    let threshold_energy = RMS_THRESHOLD_I16 * RMS_THRESHOLD_I16 * RMS_WINDOW_FRAMES as i64;
    let mut voiced_windows = 0usize;
    for window in samples.chunks_exact(RMS_WINDOW_FRAMES) {
        let energy: i64 = window
            .iter()
            .map(|sample| {
                let sample = i64::from(*sample);
                sample * sample
            })
            .sum();
        if energy >= threshold_energy {
            voiced_windows += 1;
            if voiced_windows >= MIN_VOICED_WINDOWS {
                return false;
            }
        }
    }
    true
}

fn encode_wav(samples: &[i16]) -> Vec<u8> {
    let data_bytes = samples.len().saturating_mul(2);
    let riff_size = 36usize.saturating_add(data_bytes);
    let mut wav = Vec::with_capacity(44 + data_bytes);
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(riff_size as u32).to_le_bytes());
    wav.extend_from_slice(b"WAVE");
    wav.extend_from_slice(b"fmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes());
    wav.extend_from_slice(&CHANNELS.to_le_bytes());
    wav.extend_from_slice(&SAMPLE_RATE.to_le_bytes());
    wav.extend_from_slice(&(SAMPLE_RATE * u32::from(CHANNELS) * 2).to_le_bytes());
    wav.extend_from_slice(&(CHANNELS * 2).to_le_bytes());
    wav.extend_from_slice(&BITS_PER_SAMPLE.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&(data_bytes as u32).to_le_bytes());
    for sample in samples {
        wav.extend_from_slice(&sample.to_le_bytes());
    }
    wav
}

/// 无额外时间库地把 Unix 毫秒格式化为 UTC RFC3339。
fn rfc3339_from_unix_ms(unix_ms: u64) -> String {
    let seconds = (unix_ms / 1_000) as i64;
    let millis = unix_ms % 1_000;
    let days = seconds.div_euclid(86_400);
    let seconds_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = seconds_of_day / 3_600;
    let minute = seconds_of_day % 3_600 / 60;
    let second = seconds_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}Z")
}

/// Howard Hinnant 的 civil-from-days 算法；输入为 1970-01-01 起的天数。
fn civil_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month, day)
}

#[cfg(target_os = "windows")]
mod platform {
    use super::*;
    use std::ptr;

    use windows::core::GUID;
    use windows::Win32::Media::Audio::{
        eConsole, eRender, IAudioCaptureClient, IAudioClient, IMMDeviceEnumerator,
        MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM, AUDCLNT_STREAMFLAGS_LOOPBACK,
        AUDCLNT_STREAMFLAGS_NOPERSIST, AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, WAVEFORMATEX,
        WAVE_FORMAT_PCM,
    };
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED,
    };

    const POLL_INTERVAL: Duration = Duration::from_millis(10);
    /// 100-ns units；shared-mode 下 200 ms 足以覆盖调度抖动。
    const BUFFER_DURATION_100NS: i64 = 2_000_000;

    struct ComApartment;

    impl ComApartment {
        fn initialize() -> Result<Self, String> {
            unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
                .ok()
                .map_err(|error| win_error("初始化 COM", error))?;
            Ok(Self)
        }
    }

    impl Drop for ComApartment {
        fn drop(&mut self) {
            unsafe { CoUninitialize() };
        }
    }

    pub(super) fn run(
        stop: &AtomicBool,
        command_tx: &tokio::sync::mpsc::UnboundedSender<EngineCommand>,
        ready: &mut Option<oneshot::Sender<Result<(), String>>>,
    ) -> Result<(), String> {
        let _com = ComApartment::initialize()?;
        let enumerator: IMMDeviceEnumerator = unsafe {
            CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                .map_err(|error| win_error("创建设备枚举器", error))?
        };
        let device = unsafe {
            enumerator
                .GetDefaultAudioEndpoint(eRender, eConsole)
                .map_err(|error| win_error("获取默认播放设备", error))?
        };
        let audio_client: IAudioClient = unsafe {
            device
                .Activate(CLSCTX_ALL, None)
                .map_err(|error| win_error("激活默认播放设备", error))?
        };

        let format = WAVEFORMATEX {
            wFormatTag: WAVE_FORMAT_PCM as u16,
            nChannels: CHANNELS,
            nSamplesPerSec: SAMPLE_RATE,
            nAvgBytesPerSec: SAMPLE_RATE * u32::from(CHANNELS) * 2,
            nBlockAlign: CHANNELS * 2,
            wBitsPerSample: BITS_PER_SAMPLE,
            cbSize: 0,
        };
        let stream_flags = AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
            | AUDCLNT_STREAMFLAGS_NOPERSIST;
        unsafe {
            audio_client
                .Initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    stream_flags,
                    BUFFER_DURATION_100NS,
                    0,
                    &format,
                    None,
                )
                .map_err(|error| win_error("初始化 WASAPI loopback（16kHz 单声道）", error))?;
        }
        let capture_client: IAudioCaptureClient = unsafe {
            audio_client
                .GetService()
                .map_err(|error| win_error("获取 WASAPI capture client", error))?
        };
        unsafe {
            audio_client
                .Start()
                .map_err(|error| win_error("启动 WASAPI loopback", error))?;
        }

        let startup_result = ready
            .take()
            .ok_or_else(|| "系统音频启动响应通道不存在".to_string())?
            .send(Ok(()));
        if startup_result.is_err() {
            let _ = unsafe { audio_client.Stop() };
            return Err("系统音频启动响应通道已关闭".into());
        }

        let capture_result = capture_packets(stop, command_tx, &capture_client);
        let _ = unsafe { audio_client.Stop() };
        capture_result
    }

    fn capture_packets(
        stop: &AtomicBool,
        command_tx: &tokio::sync::mpsc::UnboundedSender<EngineCommand>,
        capture_client: &IAudioCaptureClient,
    ) -> Result<(), String> {
        let mut assembler = ChunkAssembler::default();

        'capture: while !stop.load(Ordering::Acquire) {
            let mut packet_frames = unsafe {
                capture_client
                    .GetNextPacketSize()
                    .map_err(|error| win_error("读取 WASAPI packet 大小", error))?
            };
            if packet_frames == 0 {
                std::thread::sleep(POLL_INTERVAL);
                continue;
            }

            while packet_frames > 0 {
                if stop.load(Ordering::Acquire) {
                    break 'capture;
                }

                let mut data = ptr::null_mut();
                let mut frames = 0u32;
                let mut flags = 0u32;
                unsafe {
                    capture_client
                        .GetBuffer(&mut data, &mut frames, &mut flags, None, None)
                        .map_err(|error| win_error("读取 WASAPI packet", error))?;
                }

                let samples = if flags & (AUDCLNT_BUFFERFLAGS_SILENT.0 as u32) != 0 {
                    vec![0i16; frames as usize]
                } else if data.is_null() {
                    let _ = unsafe { capture_client.ReleaseBuffer(frames) };
                    return Err("WASAPI 返回了空的非静音缓冲区".into());
                } else {
                    unsafe { std::slice::from_raw_parts(data.cast::<i16>(), frames as usize) }
                        .to_vec()
                };

                unsafe {
                    capture_client
                        .ReleaseBuffer(frames)
                        .map_err(|error| win_error("释放 WASAPI packet", error))?;
                }

                for chunk in assembler.push(&samples) {
                    if stop.load(Ordering::Acquire) {
                        assembler.discard_tail();
                        break 'capture;
                    }
                    if !is_effectively_silent(&chunk) {
                        send_chunk(command_tx, &chunk)?;
                    }
                }

                packet_frames = unsafe {
                    capture_client
                        .GetNextPacketSize()
                        .map_err(|error| win_error("读取下一个 WASAPI packet", error))?
                };
            }
        }

        // 明确不冲刷不足 2.5 秒的尾片。
        assembler.discard_tail();
        Ok(())
    }

    fn send_chunk(
        command_tx: &tokio::sync::mpsc::UnboundedSender<EngineCommand>,
        samples: &[i16],
    ) -> Result<(), String> {
        let chunk_id = GUID::new()
            .map(|guid| format!("{guid:?}").to_ascii_lowercase())
            .map_err(|error| win_error("生成音频分片 ID", error))?;
        let captured_at_ms = now_ms().saturating_sub(CHUNK_DURATION_MS as u64);
        command_tx
            .send(EngineCommand::AddChunk(Box::new(NewChunk {
                chunk_id,
                // Rust outbox 是持久序号的唯一分配者，此字段仅保留兼容占位。
                chunk_seq: 0,
                captured_at: rfc3339_from_unix_ms(captured_at_ms),
                duration_ms: CHUNK_DURATION_MS,
                codec: "wav_pcm_s16le".into(),
                source: "pc".into(),
                data: encode_wav(samples),
            })))
            .map_err(|_| "Live 引擎已停止，系统音频分片无法入队".to_string())
    }

    fn win_error(context: &str, error: windows::core::Error) -> String {
        format!("{context}失败：{error}")
    }
}

#[cfg(not(target_os = "windows"))]
mod platform {
    use super::*;

    pub(super) fn run(
        _stop: &AtomicBool,
        _command_tx: &tokio::sync::mpsc::UnboundedSender<EngineCommand>,
        _ready: &mut Option<oneshot::Sender<Result<(), String>>>,
    ) -> Result<(), String> {
        Err("系统音频 loopback 仅支持 Windows".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read_u16(bytes: &[u8], offset: usize) -> u16 {
        u16::from_le_bytes([bytes[offset], bytes[offset + 1]])
    }

    fn read_u32(bytes: &[u8], offset: usize) -> u32 {
        u32::from_le_bytes([
            bytes[offset],
            bytes[offset + 1],
            bytes[offset + 2],
            bytes[offset + 3],
        ])
    }

    #[test]
    fn wav_header_describes_16k_mono_pcm_s16le() {
        let wav = encode_wav(&[i16::MIN, 0, i16::MAX]);
        assert_eq!(&wav[0..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        assert_eq!(&wav[12..16], b"fmt ");
        assert_eq!(read_u16(&wav, 20), 1);
        assert_eq!(read_u16(&wav, 22), 1);
        assert_eq!(read_u32(&wav, 24), 16_000);
        assert_eq!(read_u16(&wav, 34), 16);
        assert_eq!(&wav[36..40], b"data");
        assert_eq!(read_u32(&wav, 40), 6);
        assert_eq!(wav.len(), 50);
        assert_eq!(&wav[44..46], &i16::MIN.to_le_bytes());
        assert_eq!(&wav[48..50], &i16::MAX.to_le_bytes());
    }

    #[test]
    fn silence_gate_rejects_peaky_noise_floor_and_accepts_three_voiced_windows() {
        let mut noise_floor = vec![0i16; CHUNK_FRAMES];
        for (index, sample) in noise_floor.iter_mut().enumerate() {
            *sample = if index % 2 == 0 { 26 } else { -26 };
        }
        // 模拟 RMS 很低但偶有约 -49 dBFS 峰值的现场噪声；旧的峰值累计法会误判。
        for window in noise_floor.chunks_exact_mut(RMS_WINDOW_FRAMES) {
            window[..4].fill(116);
        }
        assert!(is_effectively_silent(&noise_floor));

        let mut only_two_voiced_windows = noise_floor.clone();
        only_two_voiced_windows[..RMS_WINDOW_FRAMES * 2].fill(512);
        assert!(is_effectively_silent(&only_two_voiced_windows));

        let mut voiced = noise_floor;
        voiced[..RMS_WINDOW_FRAMES * MIN_VOICED_WINDOWS].fill(512);
        assert!(!is_effectively_silent(&voiced));
    }

    #[test]
    fn assembler_only_emits_complete_2500ms_chunks() {
        assert_eq!(CHUNK_DURATION_MS, 2_500);
        assert_eq!(CHUNK_FRAMES, 40_000);
        let mut assembler = ChunkAssembler::default();
        assert!(assembler.push(&vec![100; CHUNK_FRAMES - 1]).is_empty());
        let completed = assembler.push(&[100]);
        assert_eq!(completed.len(), 1);
        assert_eq!(completed[0].len(), CHUNK_FRAMES);
        assert!(assembler.push(&[100; 10]).is_empty());
        assembler.discard_tail();
        assert!(assembler.samples.is_empty());
    }

    #[test]
    fn unix_epoch_formats_as_rfc3339_utc() {
        assert_eq!(rfc3339_from_unix_ms(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(
            rfc3339_from_unix_ms(1_776_297_845_123),
            "2026-04-16T00:04:05.123Z"
        );
    }
}
