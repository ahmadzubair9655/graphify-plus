package main

func produce(c chan<- int) {
	for i := 0; i < 3; i++ {
		c <- i
	}
	close(c)
}

func sum(c <-chan int) int {
	total := 0
	for v := range c {
		total += v
	}
	return total
}
