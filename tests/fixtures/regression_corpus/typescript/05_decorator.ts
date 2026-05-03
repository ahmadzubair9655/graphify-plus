function log(_t: any, _k: string, d: PropertyDescriptor) { return d; }
export class Service {
  @log
  run(): number { return 42; }
}
