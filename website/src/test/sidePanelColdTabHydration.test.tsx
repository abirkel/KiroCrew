import { useEffect } from 'react'
import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../pages/chat/FileBrowserRail', () => ({
  default: () => null,
  useTreeAvailable: () => true,
  useTreeState: () => 'ready',
}))
vi.mock('../components/MarkdownPanel', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    default: React.forwardRef(function MockMarkdownPanel() {
      return <div data-testid="markdown-panel" />
    }),
  }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'

type TabsCtl = ReturnType<typeof usePanelTabs>
let tabsCtl: TabsCtl | undefined

function Harness() {
  tabsCtl = usePanelTabs('slot-a')
  const openFile = tabsCtl.openFile
  const patchTab = tabsCtl.patchTab
  useEffect(() => {
    openFile('/repo/README.md', '# README', 'slot-a')
    patchTab('file:/repo/README.md', {
      content: undefined,
      savedContent: undefined,
      binary: undefined,
    })
  }, [openFile, patchTab])
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      projectDir="/repo"
      onFileOpen={vi.fn()}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('cold file tab hydration', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    tabsCtl = undefined
  })

  it('withholds the editor until file bytes and type are known', async () => {
    renderPanel()

    expect(await screen.findByTestId('file-tab-hydrating')).toBeInTheDocument()
    expect(screen.queryByTestId('markdown-panel')).toBeNull()

    act(() => tabsCtl?.patchTab('file:/repo/README.md', {
      content: '# README',
      savedContent: '# README',
      binary: false,
    }))

    expect(await screen.findByTestId('markdown-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('file-tab-hydrating')).toBeNull()
  })
})
