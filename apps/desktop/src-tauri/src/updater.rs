//! 自动更新模块：检查更新、下载并安装。
//!
//! Tauri 2 使用 `tauri-plugin-updater` 插件。
//! 端点和公钥配置在 `tauri.conf.json` 的 `plugins.updater` 中。
//! 注意：生产环境必须替换占位公钥为真实的签名公钥。

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Runtime};
use tauri_plugin_updater::UpdaterExt;

/// 更新检查结果，返回给前端用于展示更新状态和详情。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateStatus {
    /// 是否有可用更新
    pub update_available: bool,
    /// 当前版本
    pub current_version: String,
    /// 最新版本（如有更新）
    pub latest_version: Option<String>,
    /// 更新说明（发布日志）
    pub release_notes: Option<String>,
}

/// 检查更新并返回状态（不自动安装）。
/// 供 `#[tauri::command]` 命令调用，前端可主动触发检查。
pub async fn check_for_updates<R: Runtime>(
    app: &AppHandle<R>,
) -> Result<UpdateStatus, String> {
    let updater = app
        .updater()
        .map_err(|e| format!("获取 updater 失败: {}", e))?;
    let current_version = app.package_info().version.to_string();
    match updater.check().await {
        Ok(Some(update)) => Ok(UpdateStatus {
            update_available: true,
            current_version,
            latest_version: Some(update.version.clone()),
            release_notes: update.body.clone(),
        }),
        Ok(None) => Ok(UpdateStatus {
            update_available: false,
            current_version,
            latest_version: None,
            release_notes: None,
        }),
        Err(e) => Err(format!("检查更新失败: {}", e)),
    }
}
/// 检查更新，并在有更新时下载并安装。
/// 安装完成后通常需要重启应用以应用更新。
/// 返回 true 表示已安装更新，false 表示无可用更新。
pub async fn download_and_install<R: Runtime>(
    app: &AppHandle<R>,
) -> Result<bool, String> {
    let updater = app
        .updater()
        .map_err(|e| format!("获取 updater 失败: {}", e))?;
    match updater.check().await {
        Ok(Some(update)) => {
            // 下载并安装；两个回调分别用于进度和下载完成通知
            update
                .download_and_install(|_chunk, _total| {}, || {})
                .await
                .map_err(|e| format!("下载安装更新失败: {}", e))?;
            Ok(true)
        }
        Ok(None) => Ok(false),
        Err(e) => Err(format!("检查更新失败: {}", e)),
    }
}

/// 初始化自动更新：在后台异步执行首次更新检查。
/// 检查失败不影响应用启动。
pub fn setup<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let _ = check_for_updates(&handle).await;
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::UpdateStatus;

    #[test]
    fn update_status_serializes_to_camel_case() {
        let status = UpdateStatus {
            update_available: true,
            current_version: "0.1.0".to_string(),
            latest_version: Some("0.2.0".to_string()),
            release_notes: Some("修复若干问题".to_string()),
        };
        let json = serde_json::to_string(&status).expect("序列化失败");
        assert!(json.contains("updateAvailable"), "JSON 应包含 camelCase 字段 updateAvailable");
        assert!(json.contains("currentVersion"));
        assert!(json.contains("latestVersion"));
        assert!(json.contains("releaseNotes"));
    }

    #[test]
    fn update_status_no_update_round_trips() {
        let status = UpdateStatus {
            update_available: false,
            current_version: "0.1.0".to_string(),
            latest_version: None,
            release_notes: None,
        };
        let json = serde_json::to_string(&status).expect("序列化失败");
        let decoded: UpdateStatus = serde_json::from_str(&json).expect("反序列化失败");
        assert!(!decoded.update_available);
        assert_eq!(decoded.current_version, "0.1.0");
        assert!(decoded.latest_version.is_none());
        assert!(decoded.release_notes.is_none());
    }
}