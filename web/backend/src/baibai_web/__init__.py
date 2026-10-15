"""保存済み正本を表示する read-only application composition surface。

`baibai-web` は `127.0.0.1` にbindし、write endpoint、migration、external network
clientを持たない。画面の責務と起動手順は web/README.md、APIとserving keyの対応は
web/contracts/routes.json、画面routeは web/frontend/src/App.tsx が所有する。
"""
