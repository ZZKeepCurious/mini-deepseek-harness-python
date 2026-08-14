"""示例插件模块：被 boot() 通过 'module' 字段动态加载（第 5 章用）。"""

from .bus import Context


def apply(ctx: Context, greeting: str = "hello", service_name: str = "greeter") -> None:
    """提供服务 greeter；inject 依赖通过模块级声明。"""
    ctx.provide(service_name, lambda name: f"{greeting}, {name}!")


provides = ["greeter"]