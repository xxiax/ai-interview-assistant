/**
 * 模块解析 hook:把 TS 源码里的无扩展名相对导入补成 .ts,
 * 使 node --test 能直接加载 src/ 下的模块(包括其再导入的 bridge 等依赖)。
 */
export async function resolve(specifier, context, nextResolve) {
  const isRelative = specifier.startsWith('./') || specifier.startsWith('../')
  if (isRelative && !/\.[cm]?[jt]s$/.test(specifier)) {
    try {
      return await nextResolve(specifier + '.ts', context)
    } catch {
      // 回退原说明符,让 Node 报原始错误
    }
  }
  return nextResolve(specifier, context)
}
