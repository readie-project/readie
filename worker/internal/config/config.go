package config

import "os"

var (
	WorkerSocketDir     = os.Getenv("WORKER_SOCKET_DIR")
	AgentSocketDir      = os.Getenv("AGENT_SOCKET_DIR")
	MainNodeUri   = os.Getenv("MAIN_NODE_URI")
	WorkerNodeUri = os.Getenv("WORKER_NODE_URI")
)
