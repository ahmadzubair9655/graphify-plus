import { Greeter } from "./01_class";
export class Box<T> {
  constructor(public value: T) {}
  map<U>(fn: (v: T) => U): Box<U> { return new Box(fn(this.value)); }
}
