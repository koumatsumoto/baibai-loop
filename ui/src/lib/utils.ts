import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Render an ISO datetime stamped in JST as "YYYY-MM-DD HH:MM".
 *
 * Slicing the stored ISO string preserves the recorded wall-clock time without
 * timezone conversion, keeping publish/execution timestamps stable across viewers.
 */
export function formatJstDateTime(value: string): string {
  return value.slice(0, 16).replace('T', ' ')
}
