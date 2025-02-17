# Contributing

To make contributions to this charm, you'll need a working [development setup](https://juju.is/docs/sdk/dev-setup).

## Testing and Development

This project uses `tox` for managing test environments. There are some pre-configured environments
that can be used for linting and formatting code when you're preparing contributions to the charm.
Please see the tox.ini file in the root of this repository.

For example:

```
# Autoformat the code according to the project's guidelines
tox -e fmt

# Run linters on the code
tox -e pep8

# Run the unit tests with code coverage
tox -e cover -- tempest-k8s
```

## Build the charm

Change to the root of this repository and run:

```
tox -e build -- tempest-k8s
```
