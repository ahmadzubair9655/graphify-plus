package main

type Counter struct{ n int }

func (c *Counter) Inc() int { c.n++; return c.n }
