//! 系统托盘模块：创建托盘图标、菜单和事件处理。
//!
//! Tauri 2 使用 `TrayIconBuilder` 替代 Tauri 1.x 的 `SystemTray`。
//! 托盘菜单提供：显示窗口、隐藏窗口、退出应用。
//! 左键点击托盘图标切换主窗口显示/隐藏。

use tauri::{AppHandle, Manager, Runtime};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

/// 托盘菜单项标识常量。
pub mod menu_id {
    pub const SHOW: &str = "workama-tray-show";
    pub const HIDE: &str = "workama-tray-hide";
    pub const QUIT: &str = "workama-tray-quit";
}

pub const TRAY_ID: &str = "workama-main-tray";
const TOOLTIP: &str = "WorkAMA Desktop";

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

pub fn show_main_window<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    if let Some(window) = app.get_webview_window("main") {
        window.show()?;
        window.set_focus()?;
    }
    Ok(())
}

pub fn hide_main_window<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    if let Some(window) = app.get_webview_window("main") {
        window.hide()?;
    }
    Ok(())
}
/// 初始化系统托盘：注册图标、菜单和事件处理器。
/// 必须在 Tauri Builder 的 `setup` 回调中调用。
///
/// 菜单结构：显示窗口 / 隐藏窗口 / 分隔符 / 退出
pub fn setup<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show_item = MenuItem::with_id(app, menu_id::SHOW, "显示窗口", true, None::<&str>)?;
    let hide_item = MenuItem::with_id(app, menu_id::HIDE, "隐藏窗口", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit_item = MenuItem::with_id(app, menu_id::QUIT, "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_item, &hide_item, &separator, &quit_item])?;

    let icon = app
        .default_window_icon()
        .ok_or_else(|| anyhow::anyhow!("无法获取默认窗口图标"))?
        .clone();

    TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon)
        .tooltip(TOOLTIP)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| {
            match event.id.as_ref() {
                menu_id::SHOW => { let _ = show_main_window(app); }
                menu_id::HIDE => { let _ = hide_main_window(app); }
                menu_id::QUIT => { app.exit(0); }
                _ => {}
            }
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main_window(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{menu_id, TRAY_ID};

    #[test]
    fn menu_ids_are_non_empty_and_unique() {
        let ids = [menu_id::SHOW, menu_id::HIDE, menu_id::QUIT];
        for id in ids {
            assert!(!id.is_empty(), "托盘菜单项 ID 不应为空");
        }
        let mut sorted = ids;
        sorted.sort();
        for window in sorted.windows(2) {
            assert_ne!(window[0], window[1], "托盘菜单项 ID 不应重复");
        }
    }

    #[test]
    fn tray_id_is_non_empty() {
        assert!(!TRAY_ID.is_empty());
    }
}