package main

import (
	"fmt"
	"worker/internal/config"
	"worker/internal/rpc"

	"github.com/joho/godotenv"
)

func main() {
	err := godotenv.Load()
	if err != nil {
		fmt.Println("Error loading .env file:", err)
	}

	config.LoadConfig()

	rpc.StartServer()
}
