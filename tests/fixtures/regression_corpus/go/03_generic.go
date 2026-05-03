package main

type List[T any] struct{ items []T }

func (l *List[T]) Add(v T) { l.items = append(l.items, v) }

func (l *List[T]) Len() int { return len(l.items) }
