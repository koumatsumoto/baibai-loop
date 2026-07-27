import { Component, type ErrorInfo, type ReactNode } from 'react'

import { PageState } from './PageState'

interface Props {
  children: ReactNode
}

interface State {
  failed: boolean
}

// Serving JSON is fetched from R2 and cast, not parsed, so a view whose shape has
// moved ahead of the deployed UI throws during render — and a render throw takes
// the whole React tree with it, leaving a blank page with no way back. Deploy and
// materialize are two independent steps here, so that skew is a routine state,
// not an exotic one.
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
          title="配信データがこの UI と一致していません"
        />
      )
    }
    return this.props.children
  }
}
