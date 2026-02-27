package socket

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"time"
	"worker/internal/config"
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
	return nil, fmt.Errorf("could not connect to %s after %v", address, timeout)
}

func createSocketConnection(containerName string) error {
	dir := filepath.Join(config.WorkerSocketDir, containerName)
	os.MkdirAll(dir, 0777)
	socketPath := fmt.Sprintf("%s/executor.sock", dir)

	socket, err := dialWithRetry("unix", socketPath, 15*time.Second)
	if err != nil {
		fmt.Printf("Error connecting to socket: %v\n", err)
		return err
	}

	socketMap[containerName] = socket
	return nil
}

func GetSocketConnection(containerName string) net.Conn {
	if conn, exists := socketMap[containerName]; exists {
		return conn
	} else {
		createSocketConnection(containerName)
		return socketMap[containerName]
	}
}

func CloseSocketConnection(containerName string) {
	if conn, exists := socketMap[containerName]; exists {
		conn.Close()
		delete(socketMap, containerName)
	}
}
