package main

import "fmt"

type Animal interface{ Speak() string }

type Dog struct{ Name string }

func (d Dog) Speak() string { return fmt.Sprintf("%s barks", d.Name) }
