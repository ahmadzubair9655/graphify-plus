pub enum Shape { Circle(f64), Square(f64) }

impl Shape {
    pub fn area(&self) -> f64 {
        match self {
            Shape::Circle(r) => 3.14 * r * r,
            Shape::Square(s) => s * s,
        }
    }
}
