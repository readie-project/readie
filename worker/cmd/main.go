package main

import (
	"fmt"
	"worker/config"
	"worker/rpc"

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
