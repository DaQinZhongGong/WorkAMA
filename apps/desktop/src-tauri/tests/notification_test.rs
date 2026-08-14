//! notification 模块集成测试。
//!
//! `notification::show` 是 `tauri-plugin-notification` 的薄封装，依赖 Tauri 运行时上下文
//! （`AppHandle` + notification 插件）。完整的功能测试（实际显示通知）需要运行中的
//! Tauri 应用实例，在纯单元测试中不可行。
//!
//! 以下测试验证模块和函数的公共 API 可访问性及类型签名正确性，
//! 确保 `show` 函数的签名（参数类型、返回类型、泛型约束）符合契约。
//! 若签名意外变更（如参数类型、返回类型、可见性），这些测试将在编译期失败。

use tauri::{AppHandle, Runtime, Wry};
use workama_desktop_lib::notification;

#[test]
fn notification_module_is_publicly_accessible() {
    // `pub mod notification` 可从 crate 外部引用
    let _ = notification::show::<Wry>;
}

#[test]
fn show_function_is_publicly_exported() {
    // `pub fn show` 可通过完整路径引用
    use workama_desktop_lib::notification::show;
    let _ = show::<Wry>;
}

#[test]
fn show_function_returns_result_unit_string() {
    use workama_desktop_lib::notification::show;
    // 验证返回类型为 Result<(), String>
    let _fn: fn(&AppHandle<Wry>, &str, &str) -> Result<(), String> = show::<Wry>;
}

#[test]
fn show_function_accepts_two_str_arguments() {
    use workama_desktop_lib::notification::show;
    // 验证 title 和 body 参数类型为 &str
    fn _assert_signature(_f: fn(&AppHandle<Wry>, &str, &str) -> Result<(), String>) {}
    _assert_signature(show::<Wry>);
}

#[test]
fn show_function_is_generic_over_runtime() {
    use workama_desktop_lib::notification::show;
    // show 声明为 pub fn show<R: Runtime>(...)，验证它是泛型函数
    fn _check_generic<R: Runtime>(_f: fn(&AppHandle<R>, &str, &str) -> Result<(), String>) {}
    _check_generic::<Wry>(show::<Wry>);
}
