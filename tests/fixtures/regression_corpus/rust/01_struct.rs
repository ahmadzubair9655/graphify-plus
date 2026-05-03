pub struct Counter { n: u32 }

impl Counter {
    pub fn new() -> Self { Counter { n: 0 } }
    pub fn inc(&mut self) -> u32 { self.n += 1; self.n }
}
