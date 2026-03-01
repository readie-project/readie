package config

import "os"

const (
	ChunkSize = 1024 * 1024
)

var (
	WorkerId      string
	WorkerDir     string
	ExecutorDir   string
	MainNodeUri   string
	WorkerNodeUri string
	ExecutorImage string
)

func generateWorkerId() string {
	return "worker-1"
}

func LoadConfig() {
	WorkerId = generateWorkerId()
	WorkerDir = os.Getenv("WORKER_DIR")
	ExecutorDir = os.Getenv("EXECUTOR_DIR")
	MainNodeUri = os.Getenv("MAIN_NODE_URI")
	WorkerNodeUri = os.Getenv("WORKER_NODE_URI")
	ExecutorImage = "test-agent"
}
