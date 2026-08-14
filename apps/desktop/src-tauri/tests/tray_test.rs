//! 系统托盘集成测试。
//!
//! 验证托盘模块的公共 API 可从 crate 外部访问，
//! 且菜单项标识常量符合预期（非空、唯一、语义明确）。

use workama_desktop_lib::tray::{menu_id, TRAY_ID};

#[test]
fn tray_menu_ids_are_non_empty() {
    assert!(!menu_id::SHOW.is_empty(), "SHOW 菜单项 ID 不应为空");
    assert!(!menu_id::HIDE.is_empty(), "HIDE 菜单项 ID 不应为空");
    assert!(!menu_id::QUIT.is_empty(), "QUIT 菜单项 ID 不应为空");
}

#[test]
fn tray_menu_ids_are_unique() {
    let ids = [menu_id::SHOW, menu_id::HIDE, menu_id::QUIT];
    let mut sorted = ids;
    sorted.sort();
    for window in sorted.windows(2) {
        assert_ne!(window[0], window[1], "托盘菜单项 ID 不应重复");
    }
}

#[test]
fn tray_menu_ids_have_workama_prefix() {
    // 所有菜单项 ID 应包含项目前缀，避免与其他插件冲突
    assert!(menu_id::SHOW.starts_with("workama-"), "SHOW 应以 workama- 开头");
    assert!(menu_id::HIDE.starts_with("workama-"), "HIDE 应以 workama- 开头");
    assert!(menu_id::QUIT.starts_with("workama-"), "QUIT 应以 workama- 开头");
}

#[test]
fn tray_id_is_non_empty_and_prefixed() {
    assert!(!TRAY_ID.is_empty(), "托盘 ID 不应为空");
    assert!(TRAY_ID.starts_with("workama-"), "托盘 ID 应以 workama- 开头");
}