package socket

import (
	"fmt"
	"log"
	"net"
	"path/filepath"
	"time"
	"worker/config"
)

var (
	socketMap = make(map[string]net.Conn)
)

func dialWithRetry(network, address string, timeout time.Duration) (net.Conn, error) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout(network, address, 1*time.Second)
		if err == nil {
			return conn, nil
		}
		time.Sleep(100 * time.Millisecond)
	}
	return nil, fmt.Errorf("Could not connect to socket %s after %v", address, timeout)
}

func createSocketConnection(containerId string) error {
	socketPath := filepath.Join(config.WorkerDir, containerId, "executor.sock")
	socket, err := dialWithRetry("unix", socketPath, 15*time.Second)
	if err != nil {
		log.Printf("Error connecting to socket %s: %v", socketPath, err)
		return err
	}
	log.Printf("Connected to socket %s successfully", socketPath)

	socketMap[containerId] = socket
	return nil
}

func GetSocketConnection(containerId string) (net.Conn, error) {
	if conn, exists := socketMap[containerId]; exists {
		return conn, nil
	} else {
		err := createSocketConnection(containerId)
		if err != nil {
			return nil, err
		}
		return socketMap[containerId], nil
	}
}

func CloseSocketConnection(containerId string) {
	if conn, exists := socketMap[containerId]; exists {
		conn.Close()
		delete(socketMap, containerId)
	}
}
