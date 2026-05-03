pub struct Pair<A, B> { pub first: A, pub second: B }

impl<A, B> Pair<A, B> {
    pub fn new(a: A, b: B) -> Self { Pair { first: a, second: b } }
}
