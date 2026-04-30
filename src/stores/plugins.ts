import { LobeChatPluginManifest } from '@lobehub/chat-plugin-sdk'
import { defineStore } from 'pinia'
import { persistentReactive } from 'src/composables/persistent-reactive'
import { repos, runTx } from 'src/data'
import { GradioPluginManifest, HuggingPluginManifest, InstalledPlugin, McpPluginManifest, PluginsData } from 'src/utils/types'
import { buildLobePlugin, timePlugin, defaultData, whisperPlugin, videoTranscriptPlugin, buildGradioPlugin, calculatorPlugin, huggingToGradio, fluxPlugin, lobeDefaultData, gradioDefaultData, emotionsPlugin, mermaidPlugin, mcpDefaultData, dumpMcpPlugin, buildMcpPlugin } from 'src/utils/plugins'
import { computed } from 'vue'
import { genId } from 'src/utils/functions'
import artifacts from 'src/utils/artifacts-plugin'
import { IsTauri } from 'src/utils/platform-api'
import { useI18n } from 'vue-i18n'
import webSearchPlugin from 'src/utils/web-search-plugin'
import docParsePlugin from 'src/utils/doc-parse-plugin'

export const usePluginsStore = defineStore('plugins', () => {
  const installed = repos.installedPlugins.observeList<InstalledPlugin[]>({ initialValue: [] })
  const availableIds = computed(() => installed.value.filter(i => i.available).map(i => i.id))
  const [data, ready] = persistentReactive<PluginsData>('#plugins-data', defaultData)
  const plugins = computed(() => [
    webSearchPlugin.plugin,
    calculatorPlugin,
    videoTranscriptPlugin,
    whisperPlugin,
    fluxPlugin,
    emotionsPlugin,
    mermaidPlugin,
    timePlugin,
    docParsePlugin.plugin,
    artifacts.plugin,
    ...installed.value.map(i => {
      if (i.type === 'lobechat') return buildLobePlugin(i.manifest, i.available)
      else if (i.type === 'gradio') return buildGradioPlugin(i.manifest, i.available)
      else return buildMcpPlugin(i.manifest, i.available)
    })
  ])

  async function installLobePlugin(manifest: LobeChatPluginManifest) {
    await runTx(['installedPluginsV2', 'reactives'], async () => {
      const id = `lobe-${manifest.identifier}`
      await repos.installedPlugins.put({
        id,
        key: genId(),
        type: 'lobechat',
        available: true,
        manifest
      } as InstalledPlugin)
      await repos.reactives.update('#plugins-data', {
        [`value.${id}`]: lobeDefaultData(manifest)
      })
    })
  }

  async function installGradioPlugin(manifest: GradioPluginManifest) {
    await runTx(['installedPluginsV2', 'reactives'], async () => {
      await repos.installedPlugins.put({
        id: manifest.id,
        key: genId(),
        type: 'gradio',
        available: true,
        manifest
      } as InstalledPlugin)
      await repos.reactives.update('#plugins-data', {
        [`value.${manifest.id}`]: gradioDefaultData(manifest)
      })
    })
  }

  async function installHuggingPlugin(manifest: HuggingPluginManifest) {
    await installGradioPlugin(huggingToGradio(manifest))
  }

  const { t } = useI18n()
  async function installMcpPlugin(manifest: McpPluginManifest) {
    if (manifest.transport.type === 'stdio' && !IsTauri) throw new Error(t('stores.plugins.stdioRequireDesktop'))
    const dump = await dumpMcpPlugin(manifest)
    await runTx(['installedPluginsV2', 'reactives'], async () => {
      const plugin = await repos.installedPlugins.findFirst({ where: { id: manifest.id } })
      if (plugin) {
        await repos.installedPlugins.update(plugin.key, { type: 'mcp', available: true, manifest: dump })
      } else {
        await repos.installedPlugins.add({
          id: manifest.id,
          key: genId(),
          type: 'mcp',
          available: true,
          manifest: dump
        } as InstalledPlugin)
      }
      await repos.reactives.update('#plugins-data', {
        [`value.${manifest.id}`]: mcpDefaultData(manifest)
      })
    })
  }

  async function uninstall(id) {
    await runTx(['installedPluginsV2', 'assistants'], async () => {
      await repos.installedPlugins.modifyWhere({ where: { id } }, { available: false })
      await repos.assistants.modifyAll(a => !!a.plugins[id], { [`plugins.${id}`]: undefined })
    })
  }

  return {
    data,
    ready,
    plugins,
    availableIds,
    installLobePlugin,
    installHuggingPlugin,
    installGradioPlugin,
    installMcpPlugin,
    uninstall
  }
})
