package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"
)

const (
	socketPath = "/tmp/workama-agentd.sock"
	workspace  = "/workspace"
	maxFile    = 256 * 1024
	maxOutput  = 64 * 1024
)

type service interface{}
type agentd struct{}

func value(m *structpb.Struct, key string) string {
	if field := m.GetFields()[key]; field != nil {
		return field.GetStringValue()
	}
	return ""
}

func number(m *structpb.Struct, key string, fallback int) int {
	if field := m.GetFields()[key]; field != nil {
		return int(field.GetNumberValue())
	}
	return fallback
}

func insideWorkspace(path string) bool {
	rel, err := filepath.Rel(workspace, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func safePath(raw string, writing bool) (string, error) {
	raw = strings.ReplaceAll(raw, "\\", "/")
	for _, part := range strings.Split(raw, "/") {
		if part == ".." {
			return "", errors.New("path escapes workspace")
		}
	}
	clean := filepath.Clean("/" + raw)
	if clean == "/" || strings.Contains(raw, "\x00") {
		return "", errors.New("invalid workspace path")
	}
	full := filepath.Join(workspace, strings.TrimPrefix(clean, "/"))
	if !insideWorkspace(full) {
		return "", errors.New("path escapes workspace")
	}
	probe := full
	if writing {
		probe = filepath.Dir(full)
		if err := os.MkdirAll(probe, 0750); err != nil {
			return "", err
		}
	}
	resolved, err := filepath.EvalSymlinks(probe)
	if err != nil {
		return "", err
	}
	if !insideWorkspace(resolved) {
		return "", errors.New("symlink escapes workspace")
	}
	return full, nil
}

func response(fields map[string]any) (*structpb.Struct, error) { return structpb.NewStruct(fields) }

func health(context.Context, *structpb.Struct) (*structpb.Struct, error) {
	return response(map[string]any{"status": "ok", "service": "sandbox-agentd", "protocol": "grpc-unix", "workspace": workspace})
}

func writeFile(_ context.Context, request *structpb.Struct) (*structpb.Struct, error) {
	path, err := safePath(value(request, "path"), true)
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	data := []byte(value(request, "content"))
	if len(data) > maxFile {
		return nil, status.Error(codes.ResourceExhausted, "file exceeds 256 KiB")
	}
	if err = os.WriteFile(path, data, 0640); err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	return response(map[string]any{"path": strings.TrimPrefix(path, workspace+"/"), "size": len(data), "written": true})
}

func readFile(_ context.Context, request *structpb.Struct) (*structpb.Struct, error) {
	path, err := safePath(value(request, "path"), false)
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, status.Error(codes.NotFound, "file not found")
	}
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, maxFile+1))
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	truncated := len(data) > maxFile
	if truncated {
		data = data[:maxFile]
	}
	return response(map[string]any{"path": strings.TrimPrefix(path, workspace+"/"), "content": string(data), "size": len(data), "truncated": truncated})
}

func execute(parent context.Context, request *structpb.Struct) (*structpb.Struct, error) {
	list := request.GetFields()["argv"].GetListValue().GetValues()
	if len(list) == 0 || len(list) > 32 {
		return nil, status.Error(codes.InvalidArgument, "argv requires 1-32 values")
	}
	argv := make([]string, len(list))
	for i, item := range list {
		argv[i] = item.GetStringValue()
		if argv[i] == "" {
			return nil, status.Error(codes.InvalidArgument, "argv contains an empty value")
		}
	}
	timeout := number(request, "timeout_seconds", 10)
	if timeout < 1 || timeout > 120 {
		return nil, status.Error(codes.InvalidArgument, "timeout must be 1-120 seconds")
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(timeout)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = workspace
	cmd.Env = []string{"HOME=/home/sandbox", "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8", "PYTHONIOENCODING=utf-8"}
	output, err := cmd.CombinedOutput()
	exitCode := 0
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		} else if ctx.Err() != nil {
			exitCode = 124
		} else {
			return nil, status.Error(codes.Internal, err.Error())
		}
	}
	truncated := len(output) > maxOutput
	if truncated {
		output = output[:maxOutput]
	}
	return response(map[string]any{"exit_code": exitCode, "output": string(output), "truncated": truncated})
}

func unary(handler func(context.Context, *structpb.Struct) (*structpb.Struct, error)) grpc.MethodDesc {
	return grpc.MethodDesc{Handler: func(srv any, ctx context.Context, dec func(any) error, _ grpc.UnaryServerInterceptor) (any, error) {
		request := &structpb.Struct{}
		if err := dec(request); err != nil {
			return nil, err
		}
		return handler(ctx, request)
	}}
}

var description = grpc.ServiceDesc{
	ServiceName:  "workama.sandbox.v1.Agentd",
	HandlerType:  (*service)(nil),
	Methods: []grpc.MethodDesc{
		{MethodName: "Health", Handler: unary(health).Handler},
		{MethodName: "Exec", Handler: unary(execute).Handler},
		{MethodName: "WriteFile", Handler: unary(writeFile).Handler},
		{MethodName: "ReadFile", Handler: unary(readFile).Handler},
		{MethodName: "BrowserOp", Handler: unary(browserOp).Handler},
	},
	Streams: []grpc.StreamDesc{streamDesc()},
}

func serve() error {
	_ = os.Remove(socketPath)
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return err
	}
	if err = os.Chmod(socketPath, 0600); err != nil {
		return err
	}
	server := grpc.NewServer()
	server.RegisterService(&description, &agentd{})
	return server.Serve(listener)
}

func client(method, raw string) error {
	request := &structpb.Struct{}
	if raw != "" {
		var values map[string]any
		if err := json.Unmarshal([]byte(raw), &values); err != nil {
			return err
		}
		request, _ = structpb.NewStruct(values)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 125*time.Second)
	defer cancel()
	conn, err := grpc.NewClient("passthrough:///unix", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return net.Dial("unix", socketPath) }))
	if err != nil {
		return err
	}
	defer conn.Close()
	result := &structpb.Struct{}
	if err = conn.Invoke(ctx, "/workama.sandbox.v1.Agentd/"+method, request, result); err != nil {
		return err
	}
	encoded, err := json.Marshal(result.AsMap())
	if err == nil {
		fmt.Println(string(encoded))
	}
	return err
}

func main() {
	if len(os.Args) >= 2 && os.Args[1] == "serve" {
		if err := serve(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	if len(os.Args) >= 3 && os.Args[1] == "client" {
		raw := ""
		if len(os.Args) > 3 {
			raw = os.Args[3]
		}
		if err := client(os.Args[2], raw); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	if len(os.Args) >= 2 && os.Args[1] == "stream" {
		if err := clientStream(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	fmt.Fprintln(os.Stderr, "usage: sandbox-agentd serve | client METHOD [JSON] | stream")
	os.Exit(2)
}
