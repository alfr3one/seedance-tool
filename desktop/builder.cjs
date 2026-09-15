module.exports = {
  appId: 'local.dola.studio', productName: 'Dola Studio',
  directories: {output: 'dist-desktop'},
  files: ['desktop/main.cjs', 'package.json', '!node_modules/**/*'],
  extraResources: [
    {from: '.desktop-build/dist/dola-backend', to: 'backend'},
    {from: '.desktop-build/browsers', to: 'browsers'}
  ],
  win: {target: [{target:'nsis',arch:['x64']}], signAndEditExecutable: false},
  nsis: {
    oneClick: false,
    perMachine: false,
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: 'Dola Studio',
    deleteAppDataOnUninstall: false,
    runAfterFinish: false,
    artifactName: 'Dola-Studio-Setup-${version}-${arch}.${ext}'
  },
  asar: true
};
