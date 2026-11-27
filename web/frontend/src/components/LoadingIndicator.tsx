// Keep the status in the same space under the visible layout navigation.
export function LoadingPage({ label }: { label: string }) {
  return (
    <main className="grid min-h-[60vh] place-items-center bg-background px-6">
      <p className="grid justify-items-center gap-3 text-center text-sm text-muted-foreground" role="status">
        <span aria-hidden="true" className="size-7 rounded-full border-2 border-current border-r-transparent text-primary motion-safe:animate-spin" />
        {label}
      </p>
    </main>
  )
}
