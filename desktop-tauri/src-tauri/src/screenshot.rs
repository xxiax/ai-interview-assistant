//! 笔试辅助：抓屏并编码成 PNG。
//!
//! 只做“抓帧 + 编码”，不做 OCR，也不落盘。字节直接交给 `engine` 走
//! WebSocket 的 `solve_screenshot`，由后端的多模态 LLM 读题。截图从不写
//! 进磁盘或 SQLite，避免题面泄漏到用户机器上留痕。
//!
//! Windows 走 GDI `BitBlt` 抓整个虚拟屏（含多显示器），因为它对
//! `WDA_EXCLUDEFROMCAPTURE`（悬浮窗隐身用的那套）之外的普通窗口都有效，
//! 且不需要用户授权弹窗——面试进行中不能弹任何系统对话框。

/// 抓出来的原始帧：RGB8 紧凑排列，行优先，无 padding。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub width: u32,
    pub height: u32,
    /// 长度必须等于 `width * height * 3`。
    pub rgb: Vec<u8>,
}

/// 送进 LLM 的最长边。笔试题面在 1600px 宽下代码仍然清晰可读，
/// 再大只是把 Base64 和视觉 token 一起放大，读题准确率不再提升。
pub const MAX_EDGE: u32 = 1_600;

/// 求把最长边压到 `max_edge` 以内所需的整数降采样倍数。
///
/// 用整数倍而不是任意比例：整数倍可以直接跳像素取样，没有插值开销，
/// 也不会在代码字符的边缘引入灰边影响识别。
pub fn downscale_factor(width: u32, height: u32, max_edge: u32) -> u32 {
    let longest = width.max(height);
    if max_edge == 0 || longest <= max_edge {
        return 1;
    }
    // 向上取整除法：保证结果一定落在 max_edge 以内。
    longest.div_ceil(max_edge)
}

/// 按整数倍跳点降采样。`factor <= 1` 时原样返回。
pub fn downscale(frame: &Frame, factor: u32) -> Frame {
    if factor <= 1 {
        return frame.clone();
    }
    let width = (frame.width / factor).max(1);
    let height = (frame.height / factor).max(1);
    let mut rgb = Vec::with_capacity(width as usize * height as usize * 3);
    for y in 0..height {
        let src_row = (y * factor) as usize * frame.width as usize * 3;
        for x in 0..width {
            let src = src_row + (x * factor) as usize * 3;
            rgb.extend_from_slice(&frame.rgb[src..src + 3]);
        }
    }
    Frame { width, height, rgb }
}

/// 把 RGB8 帧编成 PNG 字节。
pub fn encode_png(frame: &Frame) -> Result<Vec<u8>, String> {
    let expected = frame.width as usize * frame.height as usize * 3;
    if frame.width == 0 || frame.height == 0 || frame.rgb.len() != expected {
        return Err("截图尺寸与像素数据不匹配".into());
    }
    let mut out = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut out, frame.width, frame.height);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        // 截图是大片纯色 + 文字，Fast 压缩已经能把 1600px 宽压到 1 MiB 内，
        // 而 Best 会多花几百毫秒——面试场景下延迟比体积重要。
        encoder.set_compression(png::Compression::Fast);
        let mut writer = encoder
            .write_header()
            .map_err(|err| format!("PNG 头写入失败：{err}"))?;
        writer
            .write_image_data(&frame.rgb)
            .map_err(|err| format!("PNG 编码失败：{err}"))?;
        writer
            .finish()
            .map_err(|err| format!("PNG 收尾失败：{err}"))?;
    }
    Ok(out)
}

/// 抓当前全部显示器并编码成 PNG。
pub fn capture_screen_png() -> Result<Vec<u8>, String> {
    let frame = platform::capture()?;
    let factor = downscale_factor(frame.width, frame.height, MAX_EDGE);
    encode_png(&downscale(&frame, factor))
}

#[cfg(target_os = "windows")]
mod platform {
    use super::Frame;

    use windows::Win32::Foundation::HWND;
    use windows::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
        GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS,
        HBITMAP, HDC, HGDIOBJ, SRCCOPY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN,
        SM_YVIRTUALSCREEN,
    };

    /// GDI 句柄的 RAII 包装：抓屏路径上有 5 个提前 return，手写释放必漏。
    struct ScreenDc(HDC);

    impl Drop for ScreenDc {
        fn drop(&mut self) {
            unsafe {
                ReleaseDC(Some(HWND::default()), self.0);
            }
        }
    }

    struct MemDc(HDC);

    impl Drop for MemDc {
        fn drop(&mut self) {
            unsafe {
                let _ = DeleteDC(self.0);
            }
        }
    }

    struct Bitmap(HBITMAP);

    impl Drop for Bitmap {
        fn drop(&mut self) {
            unsafe {
                let _ = DeleteObject(HGDIOBJ(self.0 .0));
            }
        }
    }

    pub(super) fn capture() -> Result<Frame, String> {
        unsafe {
            let left = GetSystemMetrics(SM_XVIRTUALSCREEN);
            let top = GetSystemMetrics(SM_YVIRTUALSCREEN);
            let width = GetSystemMetrics(SM_CXVIRTUALSCREEN);
            let height = GetSystemMetrics(SM_CYVIRTUALSCREEN);
            if width <= 0 || height <= 0 {
                return Err("无法获取屏幕尺寸".into());
            }

            let screen = ScreenDc(GetDC(Some(HWND::default())));
            if screen.0.is_invalid() {
                return Err("无法获取屏幕设备上下文".into());
            }
            let mem = MemDc(CreateCompatibleDC(Some(screen.0)));
            if mem.0.is_invalid() {
                return Err("无法创建兼容设备上下文".into());
            }
            let bitmap = Bitmap(CreateCompatibleBitmap(screen.0, width, height));
            if bitmap.0.is_invalid() {
                return Err("无法创建位图".into());
            }
            let previous = SelectObject(mem.0, HGDIOBJ(bitmap.0 .0));
            BitBlt(
                mem.0,
                0,
                0,
                width,
                height,
                Some(screen.0),
                left,
                top,
                SRCCOPY,
            )
            .map_err(|err| format!("抓屏失败：{err}"))?;

            // 请求 24bpp 自底向上会让 GDI 反转行序；negative height 要求
            // 自顶向下，正好和 Frame 的行优先约定一致。
            let mut info = BITMAPINFO {
                bmiHeader: BITMAPINFOHEADER {
                    biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                    biWidth: width,
                    biHeight: -height,
                    biPlanes: 1,
                    biBitCount: 24,
                    biCompression: BI_RGB.0,
                    ..Default::default()
                },
                ..Default::default()
            };
            // GDI 的 DIB 每行按 4 字节对齐，取出来后要逐行剥掉 padding。
            let stride = ((width as usize * 3) + 3) & !3;
            let mut raw = vec![0u8; stride * height as usize];
            let copied = GetDIBits(
                mem.0,
                bitmap.0,
                0,
                height as u32,
                Some(raw.as_mut_ptr().cast()),
                &mut info,
                DIB_RGB_COLORS,
            );
            SelectObject(mem.0, previous);
            if copied == 0 {
                return Err("读取位图像素失败".into());
            }

            let row_bytes = width as usize * 3;
            let mut rgb = Vec::with_capacity(row_bytes * height as usize);
            for y in 0..height as usize {
                let row = &raw[y * stride..y * stride + row_bytes];
                // GDI 给的是 BGR，PNG 要 RGB。
                for pixel in row.chunks_exact(3) {
                    rgb.extend_from_slice(&[pixel[2], pixel[1], pixel[0]]);
                }
            }
            Ok(Frame {
                width: width as u32,
                height: height as u32,
                rgb,
            })
        }
    }
}

#[cfg(not(target_os = "windows"))]
mod platform {
    use super::Frame;

    pub(super) fn capture() -> Result<Frame, String> {
        Err("截图解题仅支持 Windows".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid(width: u32, height: u32) -> Frame {
        Frame {
            width,
            height,
            rgb: (0..width * height)
                .flat_map(|index| {
                    let value = (index % 251) as u8;
                    [value, value.wrapping_add(7), value.wrapping_add(19)]
                })
                .collect(),
        }
    }

    #[test]
    fn downscale_factor_keeps_small_frames_intact() {
        assert_eq!(downscale_factor(1_280, 720, MAX_EDGE), 1);
        assert_eq!(downscale_factor(1_600, 900, MAX_EDGE), 1);
    }

    #[test]
    fn downscale_factor_rounds_up_so_result_fits_budget() {
        // 3840 / 1600 = 2.4，取 2 会留下 1920 > 1600，必须向上取 3。
        assert_eq!(downscale_factor(3_840, 2_160, MAX_EDGE), 3);
        assert_eq!(downscale_factor(5_120, 1_440, MAX_EDGE), 4);
        // 多屏横向拼接是最容易超预算的情况。
        let factor = downscale_factor(7_680, 2_160, MAX_EDGE);
        assert!(7_680 / factor <= MAX_EDGE, "降采样后仍超过最长边预算");
    }

    #[test]
    fn downscale_factor_treats_zero_budget_as_no_op() {
        assert_eq!(downscale_factor(3_840, 2_160, 0), 1);
    }

    #[test]
    fn downscale_shrinks_dimensions_and_keeps_rgb_length_consistent() {
        let frame = solid(9, 7);
        let scaled = downscale(&frame, 3);
        assert_eq!((scaled.width, scaled.height), (3, 2));
        assert_eq!(
            scaled.rgb.len(),
            scaled.width as usize * scaled.height as usize * 3
        );
        // 跳点取样：目标 (0,0) 必须等于源 (0,0)。
        assert_eq!(&scaled.rgb[0..3], &frame.rgb[0..3]);
    }

    #[test]
    fn downscale_by_one_is_identity() {
        let frame = solid(4, 4);
        assert_eq!(downscale(&frame, 1), frame);
        assert_eq!(downscale(&frame, 0), frame);
    }

    #[test]
    fn encode_png_emits_png_magic_bytes() {
        let bytes = encode_png(&solid(8, 4)).expect("编码应成功");
        assert_eq!(&bytes[..8], b"\x89PNG\r\n\x1a\n");
    }

    #[test]
    fn encode_png_rejects_mismatched_pixel_buffer() {
        let bad = Frame {
            width: 4,
            height: 4,
            rgb: vec![0; 10],
        };
        assert!(encode_png(&bad).is_err());
        let empty = Frame {
            width: 0,
            height: 0,
            rgb: Vec::new(),
        };
        assert!(encode_png(&empty).is_err());
    }
}
