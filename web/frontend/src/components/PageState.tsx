interface PageStateProps {
  message: string
  title?: string
}

export function PageState({ message, title }: PageStateProps) {
  return (
    <main className="mx-auto grid min-h-[60vh] max-w-5xl place-items-center px-6 text-center">
      <div>
        {title !== undefined && (
          <p className="text-sm font-medium text-muted-foreground">{title}</p>
        )}
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">{message}</h1>
      </div>
    </main>
  )
}
