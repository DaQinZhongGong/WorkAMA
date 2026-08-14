package observability

import (
	"context"
	"net/http"

	common "github.com/workama/workama/packages/go-common/observability"
)

var ValidRequestID = common.ValidRequestID
var RequestID = common.RequestID

func Middleware(service string, next http.Handler) http.Handler { return common.Middleware(service, next) }
func Init(ctx context.Context, service string) (func(context.Context) error, error) { return common.Init(ctx, service) }
