package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/types/known/structpb"
)

func request(values map[string]any) *structpb.Struct {
	result, _ := structpb.NewStruct(values)
	return result
}

func TestFileRoundTrip(t *testing.T) {
	_ = os.RemoveAll(filepath.Join(workspace, "agentd-test"))
	t.Cleanup(func() { _ = os.RemoveAll(filepath.Join(workspace, "agentd-test")) })
	if _, err := writeFile(context.Background(), request(map[string]any{"path": "agentd-test/state.txt", "content": "persistent"})); err != nil {
		t.Fatal(err)
	}
	result, err := readFile(context.Background(), request(map[string]any{"path": "agentd-test/state.txt"}))
	if err != nil {
		t.Fatal(err)
	}
	if value(result, "content") != "persistent" {
		t.Fatalf("unexpected content: %q", value(result, "content"))
	}
}

func TestPathTraversalRejected(t *testing.T) {
	if _, err := safePath("../../etc/passwd", false); err == nil {
		t.Fatal("expected traversal rejection")
	}
}

func TestSymlinkEscapeRejected(t *testing.T) {
	link := filepath.Join(workspace, "agentd-escape")
	_ = os.Remove(link)
	if err := os.Symlink("/etc", link); err != nil {
		t.Skip(err)
	}
	t.Cleanup(func() { _ = os.Remove(link) })
	if _, err := safePath("agentd-escape/passwd", false); err == nil || !strings.Contains(err.Error(), "escape") {
		t.Fatalf("expected symlink escape rejection, got %v", err)
	}
}

func TestExecuteUsesWorkspaceAndLimitsEnvironment(t *testing.T) {
	result, err := execute(context.Background(), request(map[string]any{"argv": []any{"sh", "-c", "pwd; printf '%s\\n' \"${DATABASE_URL:-missing}\""}, "timeout_seconds": 5}))
	if err != nil {
		t.Fatal(err)
	}
	output := value(result, "output")
	if !strings.Contains(output, workspace) || !strings.Contains(output, "missing") {
		t.Fatalf("unexpected output: %s", output)
	}
}

// boolValue 从 Struct 中提取布尔字段，缺失或类型不匹配时返回 false。
func boolValue(m *structpb.Struct, key string) bool {
	if field := m.GetFields()[key]; field != nil {
		return field.GetBoolValue()
	}
	return false
}

// ensureWorkspace 确保 /workspace 目录存在，无法创建时跳过测试。
func ensureWorkspace(t *testing.T) {
	t.Helper()
	if err := os.MkdirAll(workspace, 0750); err != nil {
		t.Skipf("workspace unavailable: %v", err)
	}
}

// workspaceDir 在 /workspace 下创建以测试名命名的临时目录，返回完整路径和相对路径。
// 测试结束自动清理。
func workspaceDir(t *testing.T) (full, rel string) {
	t.Helper()
	ensureWorkspace(t)
	full = filepath.Join(workspace, "agentd-test-"+strings.ReplaceAll(t.Name(), "/", "-"))
	if err := os.MkdirAll(full, 0750); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(full) })
	var err error
	rel, err = filepath.Rel(workspace, full)
	if err != nil {
		t.Fatal(err)
	}
	return full, rel
}

// === value / number 函数测试 ===

// value 返回存在的字符串值
func TestValueReturnsExistingString(t *testing.T) {
	m := request(map[string]any{"key": "hello"})
	if got := value(m, "key"); got != "hello" {
		t.Fatalf("expected hello, got %q", got)
	}
}

// value 在 key 不存在时返回空字符串
func TestValueReturnsEmptyForMissingKey(t *testing.T) {
	m := request(map[string]any{"other": "x"})
	if got := value(m, "key"); got != "" {
		t.Fatalf("expected empty, got %q", got)
	}
}

// value 在 field 为 nil 时返回空字符串
func TestValueReturnsEmptyForNilField(t *testing.T) {
	m := request(map[string]any{})
	if got := value(m, "key"); got != "" {
		t.Fatalf("expected empty, got %q", got)
	}
}

// number 返回存在的数字值
func TestNumberReturnsExistingValue(t *testing.T) {
	m := request(map[string]any{"count": 42.0})
	if got := number(m, "count", 0); got != 42 {
		t.Fatalf("expected 42, got %d", got)
	}
}

// number 在 key 不存在时返回 fallback
func TestNumberReturnsFallbackForMissingKey(t *testing.T) {
	m := request(map[string]any{"other": 1.0})
	if got := number(m, "count", 7); got != 7 {
		t.Fatalf("expected fallback 7, got %d", got)
	}
}

// number 在 field 为 nil 时返回 fallback
func TestNumberReturnsFallbackForNilField(t *testing.T) {
	m := request(map[string]any{})
	if got := number(m, "count", 9); got != 9 {
		t.Fatalf("expected fallback 9, got %d", got)
	}
}

// === insideWorkspace 函数测试 ===

// workspace 内的路径返回 true
func TestInsideWorkspaceTrue(t *testing.T) {
	if !insideWorkspace(filepath.Join(workspace, "subdir/file.txt")) {
		t.Fatal("expected path inside workspace to be accepted")
	}
}

// workspace 外的路径返回 false
func TestInsideWorkspaceFalse(t *testing.T) {
	if insideWorkspace("/etc/passwd") {
		t.Fatal("expected path outside workspace to be rejected")
	}
}

// 根路径返回 false
func TestInsideWorkspaceRootFalse(t *testing.T) {
	if insideWorkspace("/") {
		t.Fatal("expected root path to be rejected")
	}
}

// === safePath 函数测试 ===

// 正常路径返回成功
func TestSafePathNormal(t *testing.T) {
	dir, rel := workspaceDir(t)
	path := filepath.Join(dir, "file.txt")
	if err := os.WriteFile(path, []byte("x"), 0640); err != nil {
		t.Fatal(err)
	}
	got, err := safePath(filepath.Join(rel, "file.txt"), false)
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if got != path {
		t.Fatalf("expected %s, got %s", path, got)
	}
}

// 路径含 null 字节被拒绝
func TestSafePathRejectsNullByte(t *testing.T) {
	if _, err := safePath("file\x00.txt", false); err == nil {
		t.Fatal("expected null byte rejection")
	}
}

// 空路径被拒绝
func TestSafePathRejectsEmpty(t *testing.T) {
	if _, err := safePath("", false); err == nil {
		t.Fatal("expected empty path rejection")
	}
}

// 根路径 "/" 被拒绝
func TestSafePathRejectsRoot(t *testing.T) {
	if _, err := safePath("/", false); err == nil {
		t.Fatal("expected root path rejection")
	}
}

// writing=true 时自动创建父目录
func TestSafePathWritingCreatesDir(t *testing.T) {
	ensureWorkspace(t)
	base := filepath.Join(workspace, "agentd-test-safepath-mkdir")
	_ = os.RemoveAll(base)
	t.Cleanup(func() { _ = os.RemoveAll(base) })
	rel, err := filepath.Rel(workspace, filepath.Join(base, "nested/deep/file.txt"))
	if err != nil {
		t.Fatal(err)
	}
	full, err := safePath(rel, true)
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if _, err := os.Stat(filepath.Dir(full)); err != nil {
		t.Fatalf("expected directory created: %v", err)
	}
}

// === readFile 函数测试 ===

// 读取存在的文件返回内容
func TestReadFileExisting(t *testing.T) {
	dir, rel := workspaceDir(t)
	path := filepath.Join(dir, "file.txt")
	if err := os.WriteFile(path, []byte("hello world"), 0640); err != nil {
		t.Fatal(err)
	}
	result, err := readFile(context.Background(), request(map[string]any{"path": filepath.Join(rel, "file.txt")}))
	if err != nil {
		t.Fatal(err)
	}
	if value(result, "content") != "hello world" {
		t.Fatalf("unexpected content: %q", value(result, "content"))
	}
	if boolValue(result, "truncated") {
		t.Fatal("expected not truncated")
	}
}

// 读取不存在的文件返回错误
func TestReadFileMissing(t *testing.T) {
	ensureWorkspace(t)
	_, err := readFile(context.Background(), request(map[string]any{"path": "agentd-test-nonexistent-missing/file.txt"}))
	if err == nil {
		t.Fatal("expected error for missing file")
	}
}

// 读取超过 maxFile 的文件被截断（truncated=true）
func TestReadFileTruncatesOversize(t *testing.T) {
	dir, rel := workspaceDir(t)
	path := filepath.Join(dir, "large.txt")
	if err := os.WriteFile(path, []byte(strings.Repeat("A", maxFile+100)), 0640); err != nil {
		t.Fatal(err)
	}
	result, err := readFile(context.Background(), request(map[string]any{"path": filepath.Join(rel, "large.txt")}))
	if err != nil {
		t.Fatal(err)
	}
	if !boolValue(result, "truncated") {
		t.Fatal("expected truncated=true")
	}
	if got := len(value(result, "content")); got != maxFile {
		t.Fatalf("expected %d bytes, got %d", maxFile, got)
	}
	if number(result, "size", 0) != maxFile {
		t.Fatalf("expected size %d, got %d", maxFile, number(result, "size", 0))
	}
}

// === writeFile 函数测试 ===

// 写入新文件成功
func TestWriteFileNew(t *testing.T) {
	dir, rel := workspaceDir(t)
	result, err := writeFile(context.Background(), request(map[string]any{"path": filepath.Join(rel, "new.txt"), "content": "new content"}))
	if err != nil {
		t.Fatal(err)
	}
	if !boolValue(result, "written") {
		t.Fatal("expected written=true")
	}
	data, err := os.ReadFile(filepath.Join(dir, "new.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "new content" {
		t.Fatalf("expected new content, got %s", string(data))
	}
}

// 覆盖已有文件成功
func TestWriteFileOverwrite(t *testing.T) {
	dir, rel := workspaceDir(t)
	path := filepath.Join(dir, "overwrite.txt")
	if err := os.WriteFile(path, []byte("original"), 0640); err != nil {
		t.Fatal(err)
	}
	if _, err := writeFile(context.Background(), request(map[string]any{"path": filepath.Join(rel, "overwrite.txt"), "content": "overwritten"})); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "overwritten" {
		t.Fatalf("expected overwritten, got %s", string(data))
	}
}

// 写入超过 maxFile 的内容返回错误
func TestWriteFileTooLarge(t *testing.T) {
	_, rel := workspaceDir(t)
	_, err := writeFile(context.Background(), request(map[string]any{"path": filepath.Join(rel, "big.txt"), "content": strings.Repeat("A", maxFile+1)}))
	if err == nil {
		t.Fatal("expected error for oversize file")
	}
}

// 写入到不存在的目录时 writing=true 自动创建
func TestWriteFileCreatesDirectory(t *testing.T) {
	ensureWorkspace(t)
	base := filepath.Join(workspace, "agentd-test-writefile-mkdir")
	_ = os.RemoveAll(base)
	t.Cleanup(func() { _ = os.RemoveAll(base) })
	rel, err := filepath.Rel(workspace, filepath.Join(base, "nested/deep/file.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := writeFile(context.Background(), request(map[string]any{"path": rel, "content": "nested"})); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(workspace, rel))
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "nested" {
		t.Fatalf("expected nested, got %s", string(data))
	}
}

// === execute 函数测试 ===

// 空 argv 返回错误
func TestExecuteEmptyArgv(t *testing.T) {
	ensureWorkspace(t)
	_, err := execute(context.Background(), request(map[string]any{"argv": []any{}}))
	if err == nil {
		t.Fatal("expected error for empty argv")
	}
}

// argv 超过 32 个返回错误
func TestExecuteTooManyArgs(t *testing.T) {
	ensureWorkspace(t)
	args := make([]any, 33)
	for i := range args {
		args[i] = "x"
	}
	_, err := execute(context.Background(), request(map[string]any{"argv": args}))
	if err == nil {
		t.Fatal("expected error for too many args")
	}
}

// argv 包含空字符串返回错误
func TestExecuteEmptyArgValue(t *testing.T) {
	ensureWorkspace(t)
	_, err := execute(context.Background(), request(map[string]any{"argv": []any{"sh", ""}}))
	if err == nil {
		t.Fatal("expected error for empty arg value")
	}
}

// 超出范围的 timeout 返回错误
func TestExecuteInvalidTimeout(t *testing.T) {
	ensureWorkspace(t)
	for _, timeout := range []int{0, -1, 121, 200} {
		_, err := execute(context.Background(), request(map[string]any{"argv": []any{"echo", "hi"}, "timeout_seconds": timeout}))
		if err == nil {
			t.Fatalf("expected error for timeout %d", timeout)
		}
	}
}

// 正常执行返回输出且退出码为 0
func TestExecuteSuccess(t *testing.T) {
	ensureWorkspace(t)
	result, err := execute(context.Background(), request(map[string]any{"argv": []any{"sh", "-c", "echo hello"}, "timeout_seconds": 5}))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(value(result, "output"), "hello") {
		t.Fatalf("expected output to contain hello, got %q", value(result, "output"))
	}
	if number(result, "exit_code", -1) != 0 {
		t.Fatalf("expected exit code 0, got %d", number(result, "exit_code", -1))
	}
}

// 非零退出码被正确捕获
func TestExecuteExitCode(t *testing.T) {
	ensureWorkspace(t)
	result, err := execute(context.Background(), request(map[string]any{"argv": []any{"sh", "-c", "exit 42"}, "timeout_seconds": 5}))
	if err != nil {
		t.Fatal(err)
	}
	if number(result, "exit_code", 0) != 42 {
		t.Fatalf("expected exit code 42, got %d", number(result, "exit_code", 0))
	}
}

// === health 函数测试 ===

// health 返回 ok 状态及服务信息
func TestHealth(t *testing.T) {
	result, err := health(context.Background(), request(map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	if value(result, "status") != "ok" {
		t.Fatalf("expected status ok, got %q", value(result, "status"))
	}
	if value(result, "service") != "sandbox-agentd" {
		t.Fatalf("expected service sandbox-agentd, got %q", value(result, "service"))
	}
	if value(result, "workspace") != workspace {
		t.Fatalf("expected workspace %s, got %q", workspace, value(result, "workspace"))
	}
}

// === 服务描述符测试 ===

// 服务描述符包含预期方法且顺序正确
func TestServiceDescriptionMethods(t *testing.T) {
	expected := []string{"Health", "Exec", "WriteFile", "ReadFile", "BrowserOp"}
	if len(description.Methods) != len(expected) {
		t.Fatalf("expected %d methods, got %d", len(expected), len(description.Methods))
	}
	for i, name := range expected {
		if description.Methods[i].MethodName != name {
			t.Fatalf("method %d: expected %s, got %s", i, name, description.Methods[i].MethodName)
		}
	}
}

// 服务描述符包含 ExecStream 双向流
func TestServiceDescriptionStreams(t *testing.T) {
	if len(description.Streams) != 1 {
		t.Fatalf("expected 1 stream, got %d", len(description.Streams))
	}
	if description.Streams[0].StreamName != "ExecStream" {
		t.Fatalf("expected stream name ExecStream, got %s", description.Streams[0].StreamName)
	}
	if !description.Streams[0].ServerStreams || !description.Streams[0].ClientStreams {
		t.Fatal("expected ExecStream to be bidirectional")
	}
}

// BrowserOp 请求参数校验
func TestValidateBrowserRequest(t *testing.T) {
	cases := []struct {
		name    string
		request map[string]any
		wantErr bool
	}{
		{"navigate with target", map[string]any{"action": "navigate", "target": "https://example.com"}, false},
		{"navigate without target", map[string]any{"action": "navigate"}, true},
		{"click with target", map[string]any{"action": "click", "target": "#btn"}, false},
		{"click without target", map[string]any{"action": "click"}, true},
		{"screenshot no target needed", map[string]any{"action": "screenshot"}, false},
		{"eval no target needed", map[string]any{"action": "eval", "params": map[string]any{"expression": "1+1"}}, false},
		{"input without target", map[string]any{"action": "input"}, true},
		{"wait_for with target", map[string]any{"action": "wait_for", "target": "#result"}, false},
		{"wait_for without target", map[string]any{"action": "wait_for"}, true},
		{"close no target needed", map[string]any{"action": "close"}, false},
		{"unknown action", map[string]any{"action": "unknown"}, true},
		{"empty action", map[string]any{}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, _ := structpb.NewStruct(tc.request)
			err := validateBrowserRequest(req)
			if tc.wantErr && err == nil {
				t.Fatal("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("expected no error, got: %v", err)
			}
		})
	}
}
