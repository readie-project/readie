package config

import (
	"log"
	"os"
)

const (
	ChunkSize = 1024 * 1024
)

var (
	Port             string
	WorkerId         string
	WorkerDir        string
	MainNodeUri      string
	ExecutorImage    string
	SitePackagesPath string
)

func generateWorkerId() string {
	return "worker-1"
}

func LoadConfig() {
	WorkerId = generateWorkerId()
	Port = os.Getenv("PORT")
	WorkerDir = os.Getenv("WORKER_DIR")
	MainNodeUri = os.Getenv("MAIN_NODE_URI")
	ExecutorImage = "test-agent"

	contentBytes, err := os.ReadFile(os.Getenv("SITEPACKAGES_TXT_PATH"))
	if err != nil {
		log.Fatalf("Error in reading site-packages path file: %v", err)
	}
	SitePackagesPath = string(contentBytes)
	if SitePackagesPath == "" {
		log.Fatalf("Site packages path is empty")
	}
}
