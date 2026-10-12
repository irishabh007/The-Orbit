import unittest

from warden.agents import DeveloperAgent, TesterAgent, build_agents
from warden.agents.roles import ArchitectAgent
from warden.agents.schemas import FileBundle, SourceFile
from warden.llm import ScriptedClient
from warden.stacks import DOTNET, PYTHON, StackError, get_stack


def bundle(*paths: str, content: str = "x") -> FileBundle:
    return FileBundle(files=[SourceFile(path=p, content=content) for p in paths])


class ProfileSelectionTests(unittest.TestCase):
    def test_profiles_by_name(self) -> None:
        self.assertIs(get_stack("python-fastapi"), PYTHON)
        self.assertIs(get_stack("dotnet-aspnetcore"), DOTNET)
        with self.assertRaisesRegex(StackError, "dotnet-aspnetcore"):
            get_stack("cobol")

    def test_python_profile_changes_nothing(self) -> None:
        # the recorded greenfield run depends on these prompts being byte-identical
        agents = build_agents(ScriptedClient([]), stack=PYTHON)
        for name, cls in (("developer", DeveloperAgent), ("tester", TesterAgent), ("architect", ArchitectAgent)):
            with self.subTest(role=name):
                self.assertEqual(agents[name].task, cls.task)  # type: ignore[attr-defined]
                self.assertEqual(agents[name].role, cls.role)  # type: ignore[attr-defined]


class DotnetProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agents = build_agents(ScriptedClient([]), stack=DOTNET)

    def test_prompts_target_aspnetcore_and_xunit(self) -> None:
        self.assertIn("ASP.NET Core", self.agents["developer"].task)  # type: ignore[attr-defined]
        self.assertIn("src/App/App.csproj", self.agents["developer"].task)  # type: ignore[attr-defined]
        self.assertIn("xUnit", self.agents["tester"].task)  # type: ignore[attr-defined]
        self.assertIn("tests/Tests.csproj", self.agents["tester"].task)  # type: ignore[attr-defined]
        self.assertIn("C#", self.agents["architect"].task)  # type: ignore[attr-defined]
        self.assertIn(".NET", self.agents["developer"].role)  # type: ignore[attr-defined]

    def test_verify_runs_dotnet_build_then_test(self) -> None:
        checks = self.agents["verify"].checks  # type: ignore[attr-defined]
        self.assertEqual([c.name for c in checks], ["build", "tests"])
        self.assertTrue(all(c.argv[0] == "dotnet" for c in checks))

    def test_developer_and_tester_file_rules(self) -> None:
        dev, tester = self.agents["developer"], self.agents["tester"]
        self.assertEqual(dev.check(bundle("src/App/App.csproj", "src/App/Program.cs"), {}), [])  # type: ignore[attr-defined]
        problems = dev.check(bundle("src/App/Program.cs", "tests/X.cs"), {})  # type: ignore[attr-defined]
        self.assertTrue(any("tester" in p for p in problems))
        self.assertTrue(any("src/App/App.csproj" in p for p in problems))
        self.assertEqual(tester.check(bundle("tests/Tests.csproj", "tests/LinkTests.cs"), {}), [])  # type: ignore[attr-defined]
        self.assertTrue(any("tests/Tests.csproj" in p for p in tester.check(bundle("tests/LinkTests.cs"), {})))  # type: ignore[attr-defined]

    def test_csharp_is_left_to_the_compiler_not_python(self) -> None:
        # compile() only understands Python; C# syntax errors are caught by `dotnet build` in verify
        self.assertEqual(self.agents["developer"].builtin_checks(bundle("src/App/Program.cs", content="var x = (;")), [])  # type: ignore[attr-defined]
