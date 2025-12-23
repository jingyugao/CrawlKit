package proxygateway

import (
	"log"
	"os"
)

type Logger struct {
	inner *log.Logger
}

func NewLogger() *Logger {
	return &Logger{inner: log.New(os.Stdout, "", log.LstdFlags)}
}

func (l *Logger) Infof(format string, args ...interface{}) {
	l.inner.Printf("INFO "+format, args...)
}

func (l *Logger) Errorf(format string, args ...interface{}) {
	l.inner.Printf("ERROR "+format, args...)
}
