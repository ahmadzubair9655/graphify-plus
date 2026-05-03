package service

import "fmt"

// Handler routes incoming requests to billing.
type Handler struct {
	Name string
}

// Greet prints a greeting (fixture stand-in for a real handler).
func (h *Handler) Greet(name string) {
	fmt.Println("hello", name)
}

func NewHandler(name string) *Handler {
	return &Handler{Name: name}
}
