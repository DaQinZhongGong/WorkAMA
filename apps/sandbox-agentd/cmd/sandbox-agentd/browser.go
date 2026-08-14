package main

// browser.go — BrowserOp RPC 实现
//
// 基于 chromedp 提供沙箱内浏览器自动化能力，对齐《520》§4.3 "浏览器 CDP 桥"
// 与《710》SandboxService BrowserOp RPC 契约。
//
// 支持的 action：
//   - navigate:  导航到 URL
//   - click:     点击 CSS 选择器
//   - input:     在 CSS 选择器中输入文本
//   - screenshot: 截图（返回 base64 PNG）
//   - eval:      执行 JavaScript 表达式
//   - wait_for:  等待 CSS 选择器出现
//   - close:     关闭浏览器
//
// 协议（用 structpb.Struct 编码，与现有 unary RPC 风格一致）：
//
//	请求：
//	  {"action": "navigate", "target": "https://example.com", "timeout_ms": 10000}
//	  {"action": "click", "target": "#submit-btn", "timeout_ms": 5000}
//	  {"action": "input", "target": "#search", "params": {"text": "hello"}, "timeout_ms": 5000}
//	  {"action": "screenshot", "timeout_ms": 5000}
//	  {"action": "eval", "params": {"expression": "document.title"}, "timeout_ms": 5000}
//	  {"action": "wait_for", "target": "#result", "timeout_ms": 10000}
//	  {"action": "close"}
//
//	响应：
//	  {"ok": true, "screenshot": "<base64>", "html": "...", "meta": {"url": "...", "title": "..."}, "error": ""}

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/chromedp"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"
)

// browserPool 管理浏览器上下文（每个 agentd 进程单例，惰性启动）。
// 首次调用 BrowserOp 时启动 Chromium，后续复用；close action 或进程退出时清理。
var (
	browserMu        sync.Mutex
	browserCtx       context.Context
	browserCancel    context.CancelFunc
	browserReady     bool
	browserCtxCancel context.CancelFunc // chromedp.NewContext 返回的 cancel，关闭时调用
)

// startBrowser 启动 Chromium 并返回 chromedp context。
// 使用 NewExecAllocator 直接启动 Chromium 子进程，参数针对容器环境优化。
// parent 仅用于首次启动时的等待超时控制，不会成为 allocator 的父 context。
// 重要：allocator 的父 context 必须是 context.Background()，否则 parent 在 RPC
// handler 返回后被取消会级联取消 allocator context，进而杀死 Chromium 进程，
// 导致下一次 BrowserOp 调用拿到已取消的 browserCtx 并返回 "context canceled"。
func startBrowser(parent context.Context) (context.Context, error) {
	browserMu.Lock()
	defer browserMu.Unlock()

	if browserReady {
		return browserCtx, nil
	}

	// 创建 ExecAllocator，使用 context.Background() 作为父 context，
	// 让浏览器生命周期独立于任何单次 RPC。
	allocatorOpts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.ExecPath("/usr/bin/chromium"),
		chromedp.Flag("headless", "new"),
		chromedp.Flag("no-sandbox", true),
		chromedp.Flag("disable-gpu", true),
		chromedp.Flag("disable-dev-shm-usage", true),
		chromedp.Flag("disable-extensions", true),
		chromedp.Flag("disable-background-networking", true),
		chromedp.UserDataDir("/workspace/.chrome"),
	)

	allocCtx, allocCancel := chromedp.NewExecAllocator(context.Background(), allocatorOpts...)

	// chromedp.NewContext 返回的 cancel 必须保留，closeBrowser 时调用以释放
	// chromedp 内部资源（target listener、browser session 等）。
	// 通过 WithLogf/WithErrorf/WithDebugf 把 chromedp 内部日志写到 stderr，
	// 便于在容器内排查启动问题。
	ctx, ctxCancel := chromedp.NewContext(allocCtx,
		chromedp.WithLogf(func(format string, a ...any) {
			fmt.Fprintf(os.Stderr, "chromedp: "+format+"\n", a...)
		}),
		chromedp.WithErrorf(func(format string, a ...any) {
			fmt.Fprintf(os.Stderr, "chromedp ERROR: "+format+"\n", a...)
		}),
	)

	// 不在 startBrowser 内做 probe 导航，避免 probe ctx 的 cancel 影响 chromedp
	// 内部 target/session 状态。让首次 BrowserOp 的 chromedp.Run 惰性启动浏览器。
	// 仅校验 chromium 二进制存在且可执行。
	if _, err := os.Stat("/usr/bin/chromium"); err != nil {
		ctxCancel()
		allocCancel()
		return nil, fmt.Errorf("chromium binary not found: %w", err)
	}

	browserCtx = ctx
	browserCancel = allocCancel
	browserCtxCancel = ctxCancel
	browserReady = true
	return browserCtx, nil
}

// closeBrowser 关闭浏览器并清理资源。
func closeBrowser() {
	browserMu.Lock()
	defer browserMu.Unlock()

	if browserCtxCancel != nil {
		browserCtxCancel()
		browserCtxCancel = nil
	}
	if browserCancel != nil {
		browserCancel()
		browserCancel = nil
	}
	browserCtx = nil
	browserReady = false
}

// browserOp 是 BrowserOp RPC 的 handler。
func browserOp(parent context.Context, request *structpb.Struct) (*structpb.Struct, error) {
	action := value(request, "action")
	if action == "" {
		return nil, status.Error(codes.InvalidArgument, "action is required")
	}

	// close action 不需要浏览器运行
	if action == "close" {
		closeBrowser()
		return response(map[string]any{"ok": true})
	}

	// 启动或复用浏览器
	bctx, err := startBrowser(parent)
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}

	// 解析超时
	timeoutMs := number(request, "timeout_ms", 10000)
	if timeoutMs < 100 || timeoutMs > 60000 {
		return nil, status.Error(codes.InvalidArgument, "timeout_ms must be 100-60000")
	}

	// 重要：不使用 context.WithTimeout(bctx, ...) 派生 ctx，因为派生 ctx 的 cancel
	// 会触发 chromedp 关闭当前 target，进而导致浏览器退出（无 target 时 chromium 自动退出）。
	// 改为直接使用 bctx（chromedp context），通过 select+time.After 实现超时控制。
	// chromedp.Run 在超时后仍会继续执行直到完成或浏览器关闭，但 RPC handler 已返回。
	ctx := bctx
	timeout := time.Duration(timeoutMs) * time.Millisecond

	target := value(request, "target")
	params := request.GetFields()["params"].GetStructValue().AsMap()

	result := map[string]any{"ok": false}

	switch action {
	case "navigate":
		if target == "" {
			return nil, status.Error(codes.InvalidArgument, "target (url) is required for navigate")
		}
		// 导航并等待页面加载
		if err := runWithTimeout(ctx, timeout, chromedp.Navigate(target), chromedp.WaitReady("body")); err != nil {
			result["error"] = err.Error()
			return response(result)
		}
		// 收集页面元信息
		var url, title string
		_ = runWithTimeout(ctx, timeout,
			chromedp.Location(&url),
			chromedp.Title(&title),
		)
		result["ok"] = true
		result["meta"] = map[string]any{"url": url, "title": title}

	case "click":
		if target == "" {
			return nil, status.Error(codes.InvalidArgument, "target (css selector) is required for click")
		}
		if err := runWithTimeout(ctx, timeout, chromedp.Click(target, chromedp.ByQuery)); err != nil {
			result["error"] = err.Error()
			return response(result)
		}
		result["ok"] = true

	case "input":
		if target == "" {
			return nil, status.Error(codes.InvalidArgument, "target (css selector) is required for input")
		}
		text, _ := params["text"].(string)
		if err := runWithTimeout(ctx, timeout, chromedp.SendKeys(target, text, chromedp.ByQuery)); err != nil {
			result["error"] = err.Error()
			return response(result)
		}
		result["ok"] = true

	case "screenshot":
		// 截图，返回 base64 编码的 PNG
		var buf []byte
		if err := runWithTimeout(ctx, timeout, chromedp.CaptureScreenshot(&buf)); err != nil {
			result["error"] = err.Error()
			return response(result)
		}
		result["ok"] = true
		result["screenshot"] = base64.StdEncoding.EncodeToString(buf)

	case "eval":
		expression, _ := params["expression"].(string)
		if expression == "" {
			return nil, status.Error(codes.InvalidArgument, "params.expression is required for eval")
		}
		var evalResult any
		if err := runWithTimeout(ctx, timeout, chromedp.Evaluate(expression, &evalResult)); err != nil {
			result["error"] = err.Error()
			return response(result)
		}
		result["ok"] = true
		result["meta"] = map[string]any{"result": evalResult}

	case "wait_for":
		if target == "" {
			return nil, status.Error(codes.InvalidArgument, "target (css selector) is required for wait_for")
		}
		if err := runWithTimeout(ctx, timeout, chromedp.WaitVisible(target, chromedp.ByQuery)); err != nil {
			result["error"] = err.Error()
			return response(result)
		}
		result["ok"] = true

	default:
		return nil, status.Error(codes.InvalidArgument, fmt.Sprintf("unknown action: %s", action))
	}

	return response(result)
}

// runWithTimeout 在不取消 chromedp context 的前提下为 chromedp.Run 增加超时控制。
// chromedp 把 target 生命周期与 context 取消绑定，直接用 context.WithTimeout 派生
// ctx 会在 defer cancel() 时关闭 target，进而触发 chromium 退出。这里改为 goroutine
// + select 实现：超时返回 errors.New("timeout")，不取消 chromedp context。
// 注意：超时后 chromedp.Run 仍可能在后台执行，但由于浏览器实例为单例，后续调用会
// 串行等待（browserMu 不在此处加锁，依赖 chromedp 内部 CDP 通道串行化）。
func runWithTimeout(ctx context.Context, timeout time.Duration, actions ...chromedp.Action) error {
	type runResult struct{ err error }
	ch := make(chan runResult, 1)
	go func() {
		ch <- runResult{err: chromedp.Run(ctx, actions...)}
	}()
	select {
	case <-time.After(timeout):
		return errors.New("action timed out after " + timeout.String())
	case r := <-ch:
		return r.err
	}
}

// validateBrowserRequest 是一个轻量级校验，用于测试中验证参数合法性。
func validateBrowserRequest(request *structpb.Struct) error {
	action := value(request, "action")
	validActions := map[string]bool{
		"navigate": true, "click": true, "input": true,
		"screenshot": true, "eval": true, "wait_for": true, "close": true,
	}
	if !validActions[action] {
		return errors.New("invalid action: " + action)
	}
	requiresTarget := map[string]bool{
		"navigate": true, "click": true, "input": true, "wait_for": true,
	}
	if requiresTarget[action] {
		if strings.TrimSpace(value(request, "target")) == "" {
			return errors.New("target is required for action: " + action)
		}
	}
	return nil
}
