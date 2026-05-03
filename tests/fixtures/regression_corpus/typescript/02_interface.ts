export interface User { id: number; name: string }
export type Role = "admin" | "user";
export function isAdmin(u: User, r: Role): boolean { return r === "admin"; }
