pub trait Speak { fn speak(&self) -> String; }

pub struct Dog { pub name: String }

impl Speak for Dog {
    fn speak(&self) -> String { format!("{} barks", self.name) }
}
