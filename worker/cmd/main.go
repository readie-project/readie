package main

import (
	"log"
	"os"
	"worker/config"
	"worker/rpc"

	"github.com/joho/godotenv"
)

func main() {
	if os.Getenv("APP_ENV") != "production" {
		err := godotenv.Load()
		if err != nil {
			log.Fatalf("Error loading .env file: %v", err)
		}
	}

	config.LoadConfig()

	// TODO: Create/copy all folders on startup, checkpoints, resources

	rpc.StartServer()
}
