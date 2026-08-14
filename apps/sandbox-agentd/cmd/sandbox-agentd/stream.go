package main

// stream.go — ExecStream 双向流 RPC 实现
//
// 基于 creack/pty 提供真正的伪终端交互能力，支持：
//   - stdin/stdout/stderr 实时流式传输
//   - 窗口尺寸动态调整（resize）
//   - POSIX 信号传递（如 Ctrl+C → SIGINT）
//   - 进程退出码回报
//
// chunk 协议（用 structpb.Struct 编码，与现有 unary RPC 风格一致）：
//
//	客户端 → 服务端：
//	  {"type":"start", "argv":["bash"], "rows":24, "cols":80, "timeout_seconds":300}
//	  {"type":"input", "data":"<base64>"}
//	  {"type":"resize", "rows":30, "cols":120}
//	  {"type":"signal", "signum":2}
//
//	服务端 → 客户端：
//	  {"type":"output", "data":"<base64>"}
//	  {"type":"exit", "exit_code":0}

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"

	"github.com/creack/pty"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/structpb"
)

// execStreamChunk 表示一个双向流消息，用 structpb.Struct 编码。
// 这样可以复用现有的 value/number/response 辅助函数，无需引入 protoc 工具链。
type execStreamChunk = *structpb.Struct

// ptySession 封装单个 PTY 会话的生命周期。
type ptySession struct {
	master *os.File      // PTY master fd，用于读写子进程的 stdin/stdout/stderr
	cmd    *exec.Cmd     // 子进程
	done   chan struct{} // 子进程退出信号
	exitMu sync.Mutex
	exit   int // 退出码，-1 表示尚未退出
}

// startPTY 创建并启动一个 PTY 会话。
// 返回 master fd 和会话对象，调用方负责在结束后关闭 master。
func startPTY(argv []string, rows, cols int) (*ptySession, error) {
	if len(argv) == 0 {
		return nil, errors.New("argv is empty")
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Dir = workspace
	cmd.Env = []string{
		"HOME=/home/sandbox",
		"PATH=/usr/local/bin:/usr/bin:/bin",
		"LANG=C.UTF-8",
		"TERM=xterm-256color",
		"PYTHONIOENCODING=utf-8",
	}

	// 设置进程组，使信号可以发送到整个进程组
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	// 启动 PTY；pty.StartWithSize 会 fork+setsid+openpty+exec 一步完成
	size := &pty.Winsize{Rows: uint16(rows), Cols: uint16(cols)}
	master, err := pty.StartWithSize(cmd, size)
	if err != nil {
		return nil, err
	}

	session := &ptySession{
		master: master,
		cmd:    cmd,
		done:   make(chan struct{}),
		exit:   -1,
	}

	// 后台等待子进程退出
	go func() {
		err := cmd.Wait()
		session.exitMu.Lock()
		if err != nil {
			var exitErr *exec.ExitError
			if errors.As(err, &exitErr) {
				session.exit = exitErr.ExitCode()
			} else {
				session.exit = 137 // 被 SIGKILL 等信号终止的默认码
			}
		} else {
			session.exit = 0
		}
		session.exitMu.Unlock()
		close(session.done)
		_ = master.Close()
	}()

	return session, nil
}

// resize 动态调整 PTY 窗口尺寸。
func (s *ptySession) resize(rows, cols int) error {
	if rows < 1 || cols < 1 {
		return errors.New("invalid window size")
	}
	return pty.Setsize(s.master, &pty.Winsize{Rows: uint16(rows), Cols: uint16(cols)})
}

// sendSignal 向子进程组发送 POSIX 信号。
func (s *ptySession) sendSignal(signum int) error {
	if s.cmd.Process == nil {
		return errors.New("process not started")
	}
	// 用负 PID 向整个进程组发信号（Setpgid 确保子进程独立成组）
	return syscall.Kill(-s.cmd.Process.Pid, syscall.Signal(signum))
}

// exitCode 返回退出码，若尚未退出则返回 -1。
func (s *ptySession) exitCode() int {
	s.exitMu.Lock()
	defer s.exitMu.Unlock()
	return s.exit
}

// execStreamHandler 是 gRPC 双向流 RPC 的 handler。
//
// 协议流程：
//  1. 客户端发送 start chunk（argv + rows + cols + timeout）
//  2. 服务端启动 PTY，开始后台 goroutine 读取 master fd
//  3. 客户端可发送 input/resize/signal chunk
//  4. 服务端异步推送 output chunk（来自 master fd 的读取）
//  5. 子进程退出后服务端发送 exit chunk 并关闭流
func execStreamHandler(srv any, stream grpc.ServerStream) error {
	// 第一帧必须是 start
	var startChunk execStreamChunk = &structpb.Struct{}
	if err := stream.RecvMsg(startChunk); err != nil {
		return err
	}
	if value(startChunk, "type") != "start" {
		return errors.New("first chunk must be type=start")
	}

	// 解析 argv
	list := startChunk.GetFields()["argv"].GetListValue().GetValues()
	if len(list) == 0 || len(list) > 32 {
		return errors.New("argv requires 1-32 values")
	}
	argv := make([]string, len(list))
	for i, item := range list {
		argv[i] = item.GetStringValue()
		if argv[i] == "" {
			return errors.New("argv contains an empty value")
		}
	}

	rows := number(startChunk, "rows", 24)
	cols := number(startChunk, "cols", 80)
	timeout := number(startChunk, "timeout_seconds", 300)
	if timeout < 1 || timeout > 3600 {
		return errors.New("timeout must be 1-3600 seconds")
	}

	// 启动 PTY 会话
	session, err := startPTY(argv, rows, cols)
	if err != nil {
		return err
	}
	defer func() {
		// 确保子进程被清理
		if session.exitCode() < 0 {
			_ = session.sendSignal(int(syscall.SIGKILL))
		}
	}()

	// 超时 context
	ctx, cancel := context.WithTimeout(stream.Context(), time.Duration(timeout)*time.Second)
	defer cancel()

	// 后台 goroutine：读取 master fd 的输出，推送到客户端
	outputDone := make(chan struct{})
	go func() {
		defer close(outputDone)
		buf := make([]byte, 4096)
		for {
			n, readErr := session.master.Read(buf)
			if n > 0 {
				chunk, _ := response(map[string]any{
					"type": "output",
					"data": base64.StdEncoding.EncodeToString(buf[:n]),
				})
				_ = stream.SendMsg(chunk)
			}
			if readErr != nil {
				return
			}
			select {
			case <-ctx.Done():
				return
			default:
			}
		}
	}()

	// 主循环：读取客户端 chunk
	recvErr := make(chan error, 1)
	go func() {
		for {
			chunk := &structpb.Struct{}
			if err := stream.RecvMsg(chunk); err != nil {
				recvErr <- err
				return
			}
			switch value(chunk, "type") {
			case "input":
				raw, decErr := base64.StdEncoding.DecodeString(value(chunk, "data"))
				if decErr == nil && len(raw) > 0 {
					_, _ = session.master.Write(raw)
				}
			case "resize":
				_ = session.resize(number(chunk, "rows", 24), number(chunk, "cols", 80))
			case "signal":
				_ = session.sendSignal(number(chunk, "signum", 2))
			}
			select {
			case <-ctx.Done():
				recvErr <- ctx.Err()
				return
			default:
			}
		}
	}()

	// 等待子进程退出、超时或客户端断开
	select {
	case <-session.done:
		// 子进程自然退出
	case <-ctx.Done():
		// 超时，发送 SIGKILL
		_ = session.sendSignal(int(syscall.SIGKILL))
		<-session.done
	case err := <-recvErr:
		// 客户端断开
		if err != nil && !errors.Is(err, io.EOF) {
			_ = session.sendSignal(int(syscall.SIGTERM))
		}
		<-session.done
	}

	// 等待输出 goroutine 排空
	<-outputDone

	// 发送 exit chunk
	exitChunk, _ := response(map[string]any{
		"type":      "exit",
		"exit_code": session.exitCode(),
	})
	return stream.SendMsg(exitChunk)
}

// streamDesc 返回 ExecStream 的 StreamDesc，用于注册到 grpc.ServiceDesc。
func streamDesc() grpc.StreamDesc {
	return grpc.StreamDesc{
		StreamName:    "ExecStream",
		Handler:       execStreamHandler,
		ServerStreams: true,
		ClientStreams: true,
	}
}

// clientStream 桥接 stdin/stdout 与 gRPC 双向流。
// 用于 sandbox-agentd client stream 子命令，让 fleet 通过 docker exec 的
// stdin/stdout 管道间接连接到 agentd 的 ExecStream RPC。
func clientStream() error {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Minute)
	defer cancel()

	conn, err := grpc.NewClient("passthrough:///unix",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
			return net.Dial("unix", socketPath)
		}))
	if err != nil {
		return err
	}
	defer conn.Close()

	// 用 grpc.NewClientStream 建立双向流
	sd := streamDesc()
	stream, err := conn.NewStream(ctx, &grpc.StreamDesc{
		StreamName:    sd.StreamName,
		Handler:       nil,
		ServerStreams: sd.ServerStreams,
		ClientStreams: sd.ClientStreams,
	}, "/workama.sandbox.v1.Agentd/ExecStream")
	if err != nil {
		return err
	}

	// 从 stdin 读取 JSON 行，发送到 gRPC stream
	stdinDone := make(chan error, 1)
	go func() {
		decoder := json.NewDecoder(os.Stdin)
		for {
			var values map[string]any
			if err := decoder.Decode(&values); err != nil {
				stdinDone <- err
				return
			}
			chunk, _ := structpb.NewStruct(values)
			if err := stream.SendMsg(chunk); err != nil {
				stdinDone <- err
				return
			}
		}
	}()

	// 从 gRPC stream 读取输出，写到 stdout
	recvDone := make(chan error, 1)
	go func() {
		for {
			chunk := &structpb.Struct{}
			if err := stream.RecvMsg(chunk); err != nil {
				recvDone <- err
				return
			}
			encoded, _ := json.Marshal(chunk.AsMap())
			fmt.Println(string(encoded))
		}
	}()

	// 等待任一方结束
	select {
	case err := <-stdinDone:
		_ = stream.CloseSend()
		<-recvDone
		return err
	case err := <-recvDone:
		return err
	}
}
