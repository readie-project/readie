package config

import "os"

var (
	WorkerSocketDir   = os.Getenv("WORKER_SOCKET_DIR")
	ExecutorSocketDir = os.Getenv("EXECUTOR_SOCKET_DIR")
	MainNodeUri       = os.Getenv("MAIN_NODE_URI")
	WorkerNodeUri     = os.Getenv("WORKER_NODE_URI")
)
