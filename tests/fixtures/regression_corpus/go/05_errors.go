package main

import "errors"

var ErrNotFound = errors.New("not found")

func Lookup(id int) (string, error) {
	if id == 0 {
		return "", ErrNotFound
	}
	return "ok", nil
}
