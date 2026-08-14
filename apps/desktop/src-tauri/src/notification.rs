//! 原生通知模块：通过系统通知中心显示消息。
//!
//! Tauri 2 使用 `tauri-plugin-notification` 插件。
//! 通知模块是插件 API 的薄封装，依赖 Tauri 运行时上下文，
//! 纯逻辑测试在集成测试 `tests/config_test.rs` 中覆盖。

use tauri::{AppHandle, Runtime};
use tauri_plugin_notification::NotificationExt;

/// 显示系统通知。
///
/// `title` 为通知标题，`body` 为通知正文。
/// 供 `#[tauri::command]` 命令和内部模块调用。
pub fn show<R: Runtime>(
    app: &AppHandle<R>,
    title: &str,
    body: &str,
) -> Result<(), String> {
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|e| format!("显示通知失败: {}", e))?;
    Ok(())
}