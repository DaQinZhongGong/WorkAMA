//! 全局快捷键模块：注册全局快捷键以快速唤起主窗口。
//!
//! Tauri 2 使用 `tauri-plugin-global-shortcut` 插件。
//! 默认快捷键：Ctrl+Shift+W 切换主窗口显示/隐藏。

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

/// 默认全局快捷键：Ctrl+Shift+W
pub const DEFAULT_SHORTCUT: &str = "Ctrl+Shift+W";

/// 快捷键配置（可序列化，便于前端通过命令读写和持久化）。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ShortcutConfig {
    /// 是否启用全局快捷键
    pub enabled: bool,
    /// 快捷键组合，例如 "Ctrl+Shift+W"
    pub accelerator: String,
}

impl Default for ShortcutConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            accelerator: DEFAULT_SHORTCUT.to_string(),
        }
    }
}

fn toggle_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}
/// 初始化全局快捷键：根据默认配置注册快捷键。
/// 必须在 Tauri Builder 的 `setup` 回调中调用。
pub fn setup<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let config = ShortcutConfig::default();
    if !config.enabled {
        return Ok(());
    }
    let shortcut: Shortcut = config
        .accelerator
        .parse()
        .map_err(|e| anyhow::anyhow!("解析快捷键失败: {}", e))?;
    app.global_shortcut()
        .on_shortcut(shortcut, move |app, _shortcut, event| {
            if event.state == ShortcutState::Pressed {
                toggle_main_window(app);
            }
        }).map_err(anyhow::Error::from)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{ShortcutConfig, DEFAULT_SHORTCUT};

    #[test]
    fn default_config_is_enabled_with_default_accelerator() {
        let config = ShortcutConfig::default();
        assert!(config.enabled, "默认应启用全局快捷键");
        assert_eq!(config.accelerator, DEFAULT_SHORTCUT);
        assert_eq!(config.accelerator, "Ctrl+Shift+W");
    }

    #[test]
    fn config_serializes_to_camel_case() {
        let config = ShortcutConfig::default();
        let json = serde_json::to_string(&config).expect("序列化失败");
        assert!(json.contains("enabled"), "JSON 应包含 enabled 字段");
        assert!(json.contains("accelerator"), "JSON 应包含 accelerator 字段");
    }

    #[test]
    fn config_round_trips_through_json() {
        let config = ShortcutConfig::default();
        let json = serde_json::to_string(&config).expect("序列化失败");
        let decoded: ShortcutConfig = serde_json::from_str(&json).expect("反序列化失败");
        assert_eq!(decoded.enabled, config.enabled);
        assert_eq!(decoded.accelerator, config.accelerator);
    }

    #[test]
    fn custom_config_can_disable_shortcut() {
        let json = r#"{"enabled":false,"accelerator":"Alt+Space"}"#;
        let config: ShortcutConfig = serde_json::from_str(json).expect("反序列化失败");
        assert!(!config.enabled);
        assert_eq!(config.accelerator, "Alt+Space");
    }
}