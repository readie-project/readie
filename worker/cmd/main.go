package main

import (
	"log"
	"worker/config"
	"worker/rpc"

	"github.com/joho/godotenv"
)

func main() {
	err := godotenv.Load()
	if err != nil {
		log.Fatalf("Error loading .env file: %v", err)
	}

	config.LoadConfig()

	// TODO: Create/copy all folders on startup, checkpoints, resources

	rpc.StartServer()
}
