/** Explicit image directory for the unchanged Web load_image/dump_image commands. */
import {readFile,open,unlink,realpath} from 'node:fs/promises';
import {resolve,relative,isAbsolute,dirname,sep} from 'node:path';
export async function localImages(directory){
  const root=await realpath(directory);
  function within(path,allowRoot=false){
    const rel=relative(root,path);
    if((!rel&&!allowRoot)||rel==='..'||rel.startsWith('..'+sep)||isAbsolute(rel))throw Error('镜像路径必须在 --image-root 内');
    return path;
  }
  return {
    async read(name){return new Uint8Array(await readFile(within(await realpath(resolve(root,name)))));},
    async openWrite(name){
      const dest=within(resolve(root,name));within(await realpath(dirname(dest)),true);
      const handle=await open(dest,'wx');let complete=false,closed=false;
      const close=async()=>{if(!closed){await handle.close();closed=true;}};
      return {
        write:bytes=>handle.writeFile(bytes),
        async close(){await handle.sync();await close();complete=true;},
        async abort(){await close();if(!complete){await unlink(dest);complete=true;}},
      };
    },
  };
}
