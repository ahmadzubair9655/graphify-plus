export class Greeter {
  constructor(private name: string) {}
  greet(): string { return `hello ${this.name}`; }
}
