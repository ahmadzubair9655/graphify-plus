import { Animal } from "./02_class";
export class Dog extends Animal {
  speak() { return `${this.name} barks`; }
}
