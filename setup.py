from setuptools import find_packages, setup


plugin_identifier = "rme_compatibility"
plugin_package = "octoprint_rme_compatibility"


setup(
    name="OctoPrint-RMECompatibility",
    version="0.1.0b22",
    description="OctoPrint support for Prusa RME firmware",
    author="Rylan Meilutis and RME contributors",
    author_email="rylan.meilutis@gmail.com",
    url="https://github.com/Rylan-Meilutis/OctoPrint-RME-Compatibility",
    license="AGPLv3",
    packages=find_packages(),
    include_package_data=True,
    python_requires=">=3.8",
    install_requires=["requests>=2.20,<3"],
    entry_points={
        "octoprint.plugin": [
            "%s = %s" % (plugin_identifier, plugin_package),
        ]
    },
)
