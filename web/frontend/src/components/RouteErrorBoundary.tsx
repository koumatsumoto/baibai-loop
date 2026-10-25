import { Component, type ErrorInfo, type ReactNode } from 'react'

import { PageState } from './PageState'

interface Props {
  children: ReactNode
}

interface State {
  failed: boolean
}

// Rendering and lazy-module failures share this boundary. The cause is logged;
// the fallback must not assume a particular data or deployment mismatch.
export class RouteErrorBoundary extends Component<Props, State> {
  override state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('route render failed', error, info.componentStack)
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <PageState
          message="この画面を表示できませんでした"
          title="ページを再読み込みしてください。改善しない場合は時間をおいて再度お試しください"
        />
      )
    }
    return this.props.children
  }
}
